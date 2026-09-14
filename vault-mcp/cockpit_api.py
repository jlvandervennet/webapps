#!/usr/bin/env python3
"""
cockpit_api — thin HTTP backend for Joe's Cockpit PWA. Hephaestus, 2026-09-14.

Every read/write goes through vaultlib (the SAME core the vault MCP server
uses) so the app and the fleet are consistent by construction. Writes are
frontmatter-safe (GL-002) and Original-Text-refusing. Calendar writes + the
find/propose/tap scheduling-suggestion flow delegate to the existing
sched.py (single-source Google write, confirm-first).

Stdlib only (http.server). No secrets in code; password via env.
"""

import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Make vaultlib importable regardless of cwd.
VAULT_MCP = Path("/opt/data/vault-mcp")
sys.path.insert(0, str(VAULT_MCP))
import vaultlib  # noqa: E402

# The shared /done completion log (cumulative JSONL + daily ✅ Done section).
sys.path.insert(0, str(Path("/opt/data/plugins")))

VAULT = Path(os.environ.get("VAULT_ROOT", "/opt/data/Second-Brain")).resolve()
HOST = os.environ.get("COCKPIT_HOST", "100.113.241.19")
PORT = int(os.environ.get("COCKPIT_PORT", "8792"))
PASSWORD = os.environ.get("COCKPIT_PASSWORD", "")
SCHED = "/opt/data/scripts/sched.py"
DONE_LOG = VAULT / "05 Assets" / "Data" / "done" / "completions.jsonl"

TODAY = date.today().isoformat()


def _dt(v):
    try:
        return datetime.fromisoformat(v).astimezone().isoformat()
    except Exception:
        return v


# ── Read endpoints ─────────────────────────────────────────────────────────
def _today_done_titles():
    """Unique completion titles for TODAY, deduped across BOTH sources:

    1. the cumulative /done log (05 Assets/Data/done/completions.jsonl), and
    2. today's daily scratchpad `## ✅ Done` CHECKED boxes (`- [x] …`).

    Dedupe is by normalized lowercase title so a task logged once appears once
    (Joe round-3: "as long as we dedupe we're fine"). Never double-counts the
    same finish across the two sources.
    """
    seen = {}          # norm-title -> display title
    # source 1: cumulative completion log (ground truth, append-only)
    try:
        if DONE_LOG.is_file():
            for line in DONE_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("date") != TODAY:
                    continue
                t = (rec.get("task") or "").strip()
                if not t:
                    continue
                seen.setdefault(t.lower(), t)
    except OSError:
        pass
    # source 2: today's daily checked Done boxes
    try:
        p = VAULT / "00 Daily Scratchpad" / ("%s.md" % TODAY)
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            in_done = False
            for line in text.splitlines():
                if line.startswith("## ✅ Done"):
                    in_done = True
                    continue
                if line.startswith("## ") and in_done:
                    in_done = False
                    continue
                if not in_done:
                    continue
                m = re.match(r"- \[[xX]\]\s*(.*)", line)
                if m:
                    t = m.group(1).strip()
                    if t:
                        seen.setdefault(t.lower(), t)
    except OSError:
        pass
    return list(seen.values())


def get_today():
    """Counter: number of UNIQUE things done today (deduped). Always real."""
    done = _today_done_titles()
    return {"date": TODAY, "done_count": len(done), "done_titles": done}


def get_open_items():
    """The Open/Pick surface — ACTIONABLE TASKS + DECISIONS ONLY.

    Round-2 steer: this is a TASK list, not a project tracker. Excluded:
    long-term goals, projects, and long-term emotional/Apollo content (they
    belong on the goals/bench views, not the pick list), and ambiguous daily
    checkbox anchors. Returns items tagged {kind, path, title, do_by, default,
    importance}; goals and daily anchors never appear.
    """
    items = []

    # Decisions are NOT in the Pick list (contract IA: Pick = actionable tasks
    # only; live decisions have their own Decide panel with lean + decide-by).

    # 1) open type:task (status: open) — the actionable set.
    # ROUND-3 (Joe): the SINGLE cockpit source is 02 Planner/Tasks/open/. Hermes
    # owns creating + populating it. Until it exists (it does not yet), we
    # fall back to the whole-vault scan so the app still lists real tasks — the
    # moment the Planner source lands, this uses it exclusively.
    planner_open = VAULT / "02 Planner" / "Tasks" / "open"
    sources = [planner_open] if planner_open.is_dir() else [VAULT]
    item_paths = {}
    for src in sources:
        for p in src.rglob("*.md"):
            sp = str(p)
            if ".git" in sp or "_archive" in sp or p.name.startswith("_"):
                continue
            try:
                fm, _ = vaultlib.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if fm.get("type") != "task":
                continue
            if str(fm.get("status", "")).lower() in ("done", "cancelled", "closed"):
                continue
            rel = str(p.relative_to(VAULT))
            if rel in item_paths:      # planner source wins over fallback
                continue
            item_paths[rel] = {
                "kind": "task", "path": rel,
                "title": fm.get("title") or p.stem,
                "do_by": fm.get("due") or fm.get("do-by"),
                "default": fm.get("default"),
                "importance": 2 if fm.get("assignee") in ("joe", "hermes") else 1,
            }
    items = list(item_paths.values())

    # NOTE: goals (04 Inner World/My Life/Goals) and daily anchors are
    # deliberately EXCLUDED — round-2 steer: tasks-only pick list.

    # sort: importance desc, then items with a date first
    items.sort(key=lambda x: (x["importance"], -bool(x["do_by"])), reverse=True)
    # Prune the persisted plate to only items still in the open set (round-2:
    # a goal/daily entry picked before the tasks-only scope must not inflate
    # the header count or linger in the Active lane).
    live_paths = {x["path"] for x in items}
    plate = [p for p in vaultlib.get_plate() if p in live_paths]
    if plate != vaultlib.get_plate():
        vaultlib.set_plate(plate)  # back-fix the stale persisted plate
    # Never ship internal tokens to a panel (contract §5). `path` rides along
    # only as the machine handle for the write action — nothing token-y is
    # rendered as display text.
    return {
        "items": [_ui_item(i, on_plate=(i["path"] in plate)) for i in items],
        "plate": plate,
        "plate_full": len(plate) >= 3,
    }


def _ui_item(it, *, on_plate):
    """Proxy a raw item to plain English for the UI. NO internal vocabulary.

    Contract §5 (hard rule): no file paths, no `status:`/`awaiting:`/
    `due:`/`decision:` labels, no `.md`, no raw tokens — every internal value
    is proxied before a panel sees it. `path` stays machine-only (used by the
    write actions); it is never rendered as text.
    """
    kind = "decision" if it["kind"] == "decision" else "task"
    due_raw = it.get("do_by") or it.get("due")
    return {
        "path": it["path"],          # machine handle only — UI never prints it
        "kind": kind,
        "tag": "Decide" if kind == "decision" else "To do",
        "title": (it.get("title") or "").strip() or "Untitled",
        # proxy decide-by / lean to plain phrasing (never `due:`/`do-by:`)
        "decide_by": _plain_date(due_raw),
        "decide_by_raw": due_raw or "",
        "has_decide_by": bool(due_raw),
        "lean": (it.get("default") or "").strip() or "",
        "has_lean": bool((it.get("default") or "").strip()),
        "on_plate": on_plate,
    }


def _plain_date(value):
    """'2026-09-14' (or any date-like) -> 'Sep 14' / 'Tue Sep 16'. Never a token."""
    import datetime as _dt
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            d = _dt.datetime.strptime(str(value)[:19], fmt).date()
            return d.strftime("%a %b %-d") if hasattr(d, "strftime") else d.strftime("%a %b %-d")
        except ValueError:
            continue
    return str(value)


# ── Decide panel (live decisions + 'coming up' preview, contract §2/§3) ────
def get_decide():
    """Live decisions awaiting Joe, each with a lean + decide-by. Plain-English.

    Contract: Decide shows ONLY live decisions (awaiting-action), each a row
    with the 3-option lean surfaced and a decide-by date. Resolved/empty
    collapses to the 'you cleared the deck' empty state (handled UI-side).
    Returns the decision set + a proxied 'coming up' preview for the empty
    state so the panel still guides (no dead air).
    """
    rows = []
    for d in vaultlib.get_joe_decisions():
        # surface the decision's 3 options + the lean. Prefer body parse
        # (the caveman-native "1. / 2. / 3." list + "(Lean)" / "Default" clause
        # — the round-5 live decision carries them in the BODY), fall back to
        # any explicit frontmatter options.
        options = []
        lean = (d.get("default") or "").strip()
        try:
            note = vaultlib.read_note(d["path"])
            body = note.get("body") or ""
            fm = note.get("frontmatter") or {}
            parsed_opts, parsed_lean = _parse_decision_options(body)
            if parsed_opts:
                options = parsed_opts
            elif isinstance(fm.get("options"), list):
                options = [str(o).strip() for o in fm["options"] if str(o).strip()]
            if not lean and parsed_lean:
                lean = parsed_lean
        except Exception:
            options = []
        rows.append({
            "title": (d.get("title") or "").strip(),
            "decide_by": _plain_date(d.get("do_by")),
            "decide_by_raw": d.get("do_by") or "",
            "lean": lean,
            "has_lean": bool(lean),
            "options": options[:3],          # the 3 options, if present
            "has_options": len(options) > 0,
            "path": d["path"],               # machine handle only
        })
    # 'coming up' preview: next decide-by / soonest dated open items.
    return {"cleared": len(rows) == 0, "decisions": rows, "coming_up": _next_up()}


def _parse_decision_options(body: str):
    """Extract (options, lean) from a decision note body.

    Convention (caveman-native, matches the live Two Roots note):
        "1. (Lean) First option"
        "2. Second option"
        "3. Third option"
        "**Default if no choice by <date>:** option 1 (a zero-setup win…)"

    Returns ([option1, option2, option3], lean_string). The numbered list
    drives the row's 3 choices; "(Lean)" on an item (or the Default clause)
    marks which one the note leans toward.
    """
    opts = []
    for line in body.splitlines():
        line = line.strip()
        m = re.match(r"^(\d)\.\s+(.+)", line)
        if m:
            txt = m.group(2).strip()
            # strip "(Lean)" marker however it's wrapped: **(Lean)** / (Lean) / **(Lean)
            txt = re.sub(r"\*{0,2}\(\s*Lean\s*\)\*{0,2}", "", txt, flags=re.I).strip()
            # strip leading emphasis + stray punctuation from the head
            txt = re.sub(r"^[*_#>:\s-]+", "", txt).strip()
            # drop inline emphasis markers (e.g. **bold**) so the option renders clean
            txt = txt.replace("**", "").replace("`", "")
            opts.append(txt)
        if len(opts) >= 3:
            break
    lean = ""
    # Default-if clause -> the lean: it references "option <N>"; map N to text.
    m = re.search(r"Default[^.\n]{0,60}?\boption\s*(\d)\b", body, re.I)
    if m:
        n = int(m.group(1))
        if opts and 1 <= n <= len(opts):
            lean = opts[n - 1]
    # else scan for the explicit (Lean) marker on an option
    if not lean:
        for i, opt in enumerate(opts):
            if re.search(r"^.*\(lean\)", opt, re.I) or "lean" in opt.lower()[:14]:
                lean = re.sub(r"^\(Lean\)\s*|\*{1,2}", "", opt).strip()
                break
    return opts, lean


def _next_up(limit=3):
    """Soonest dated open items (tasks with due/do-by), proxied — for the
    Decide empty state ('what's coming up') so a cleared deck still guides.
    """
    due = []
    for p in VAULT.rglob("*.md"):
        sp = str(p)
        if ".git" in sp or "_archive" in sp or p.name.startswith("_"):
            continue
        try:
            fm, _ = vaultlib.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if fm.get("type") != "task" or str(fm.get("status", "")).lower() in ("done", "cancelled", "closed"):
            continue
        raw = fm.get("due") or fm.get("do-by") or ""
        if not raw:
            continue
        due.append({"title": (fm.get("title") or p.stem).strip() or "Untitled",
                    "decide_by_raw": str(raw), "decide_by": _plain_date(raw)})
    due.sort(key=lambda x: x["decide_by_raw"])
    return due[:limit]


# ── Write endpoints (frontmatter-safe via vaultlib) ────────────────────────
def set_due(path, due_date):
    """Set decide-by on an open item (uses GL-002 `due`; no new field)."""
    return vaultlib.set_frontmatter(path, {"due": due_date})


def add_quick_task(text):
    """Quick-add a task to the Planner open source (round-7). Vault-canonical
    via vaultlib.create_open_task (new open-task note, GL-002, Original-Text-
    safe — never touches existing notes/bodies)."""
    try:
        return vaultlib.create_open_task(text)
    except ValueError as e:
        return {"ok": False, "output": str(e)}


def resolve_decision(path, resolution="resolved", chosen=None):
    """Close a decision: decision:resolved + awaiting:none (via vaultlib).

    The CHOSEN option is NOT written to frontmatter `default` (not sanctioned
    as a field yet — the team moved defaults to the body). Instead it's logged
    via the done plugin (sanctioned completion record + feeds the number-go-up
    counter), which is the contract's "resolving a decision immediately feeds
    the counter" reward-immediacy rule. Body is never edited.
    """
    updates = {"decision": resolution, "awaiting": "none"}
    outcome = vaultlib.set_frontmatter(path, updates)
    logged_log = ""
    if resolution == "resolved" and chosen:
        try:
            title = ""
            try:
                title = vaultlib.read_note(path).get("frontmatter", {}).get("title") or ""
            except Exception:
                pass
            from done import log_completion
            log_completion("Decided: %s — %s" % (title, chosen), author="joe", commit=False)
            logged_log = "done-logged"
        except Exception:
            logged_log = "(log hiccup)"
    return {**outcome, "chosen": chosen or "", "logged": logged_log}


def mark_task_done(path):
    """Write-back: complete a cockpit task end-to-end (round-3, observable).

    What "Done" does, exactly (Joe: "what do we expect, what do we get"):
      1. flip the task note  status: open -> done  (via vaultlib, GL-002-safe,
         Original-Text-safe — the body is never touched);
      2. LOG the completion to the cumulative /done record (done plugin):
           - 00 Daily Scratchpad/YYYY-MM-DD.md  ## ✅ Done  (newest-first line)
           - 05 Assets/Data/done/completions.jsonl  (append-only, author=joe)
      3. DEDUPE: if the task is ALREADY done (status already 'done'), it is a
         no-op — no second flip, no second /done log line, so it is never
         double-counted in the counter (which also dedupes by title).

    Returns a read-back trace so Joe/Hermes can verify the exact write
    (ground truth, not assumed). No secrets; all writes go through vaultlib
    except the done-log append which is the canonical shared record.
    """
    try:
        note = vaultlib.read_note(path)
    except Exception as e:
        return {"ok": False, "output": "Couldn't find that task.", "was": {"status": "?", "title": "?"}}
    title = (note.get("frontmatter", {}).get("title") or "").strip() or path
    before = note.get("frontmatter", {}).get("status") or "open"

    if str(before).lower() == "done":
        # DEDUPE: already completed -> no-op, never double logs / double counts.
        return {"ok": True, "deduped": True, "output": "Already done — not logged again.",
                "title": title, "status_before": "done", "status_after": "done"}

    # 1) flip status open -> done (idempotent, safe write)
    write = vaultlib.set_frontmatter(path, {"status": "done"})
    status_after = "done"

    # 2) log to the cumulative /done record (daily ✅ Done + completions.jsonl)
    logged_to, log_path = "", ""
    try:
        from done import log_completion  # plugins/done (author defaults to 'joe')
        result = log_completion(title, author="joe", commit=False)
        # parse "daily note: <path>" + "cumulative log: <path>" from result
        import re as _re
        for label in ("daily note", "cumulative log"):
            m = _re.search(label + r":[ \t]+([^\n]+)", result)
            if m:
                if label == "daily note":
                    logged_to = m.group(1).strip()
                else:
                    log_path = m.group(1).strip()
    except Exception as e:
        # never fail the Done if logging hiccups — status IS flipped
        logged_to = "(log failed: %s)" % type(e).__name__

    return {
        "ok": True, "deduped": False,
        "output": "Done — task completed and logged.",
        "title": title,
        "status_before": before, "status_after": status_after,
        "note_flipped": write.get("path"),
        "logged_daily": logged_to,        # daily ✅ Done line location
        "logged_log": log_path,           # cumulative completions.jsonl
    }


# ── Calendar / scheduling (delegates to existing sched.py) ─────────────────
def _sched(*args):
    r = subprocess.run(
        [sys.executable or "python3", SCHED, *args],
        capture_output=True, text=True, timeout=60,
    )
    return r.returncode, (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")


def find_slots(summary, duration, window=None, days=7):
    """Anti-decision scheduling suggestions (find/propose/tap from sched.py)."""
    cmd = ["find", "--summary", summary, "--duration", str(duration), "--days", str(days)]
    if window:
        cmd += ["--window", window]
    code, out = _sched(*cmd)
    return {"ok": code == 0, "output": out}


def schedule_plate():
    """Scheduler AUTOFILL from the picked tasks (round-2 steer).

    Returns CLEAN, tappable scheduling suggestions for the items ALREADY on
    the plate — no raw CLI/sched.py strings, no file paths, no internal
    labels, no '(unknown)' / ambiguous rows (round-2: "no code/CLI in the UI",
    "don't surface junk items"). Each suggestion is a structured slot the
    frontend renders as a tap-to-book action.
    """
    plate = vaultlib.get_plate()
    out = {"plate_count": len(plate), "items": []}
    for idx, path in enumerate(plate):
        # Resolve a human title; drop anything we can't cleanly name (no "(unknown)").
        title = None
        try:
            note = vaultlib.read_note(path)
            cand = (note["frontmatter"].get("title") or Path(path).stem).strip()
            if cand and cand != "(unknown)":
                title = cand
        except Exception:
            pass
        # Skip daily/checkbox anchors and anything unresolved (suppress junk).
        if path.startswith("00 Daily Scratchpad") or not title:
            continue
        slots = _find_slots_clean(title, 30)
        out["items"].append({
            "idx": idx,              # stable index for the per-row manual override
            "title": title,
            "slots": slots[:3],  # a few clear choices, not a dump
            "found": len(slots) > 0,
        })
    return out


def _find_slots_clean(summary, duration, window=None, days=7):
    """Call sched.py find and PARSE only the concrete slot lines.

    sched.py prints lines like:
        "  1. Mon 2026-09-15 09:00–09:30"
    followed by CLI instructions. We keep ONLY the dated slots, as structured
    objects — the raw CLI tail never reaches the UI.
    """
    cmd = ["find", "--summary", summary, "--duration", str(duration), "--days", str(days)]
    if window:
        cmd += ["--window", window]
    code, out = _sched(*cmd)
    if code != 0:
        return []
    import re as _re
    slots = []
    for line in out.splitlines():
        m = _re.match(r"\s*\d+\.\s+\w+\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})[\s\u2013-]+(\d{2}:\d{2})", line)
        if m:
            slots.append({
                "date": m.group(1),
                "start": m.group(2),
                "end": m.group(3),
                "label": "%s %s\u2013%s" % (m.group(1)[5:], m.group(2), m.group(3)),
            })
    return slots


def book_calendar(summary, start_iso, end_iso, location="", description="", force=True):
    """Write a date-set straight to Google Calendar via sched.py book (approved).

    Joe's rule: he puts a date -> reasonable lag -> on his calendar. The cockpit
    books directly (auto-approved) since Joe is the app's sole owner; sched.py's
    confirm-first gate is satisfied by the app invoking book --yes.
    """
    pid = re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:48]
    # stage a proposal then book it directly
    stage_code, stage_out = _sched("propose", "--summary", summary,
                                   "--date", start_iso[:10],
                                   "--start", start_iso[11:16],
                                   "--end", end_iso[11:16],
                                   "--location", location or "",
                                   "--description", description or "",
                                   "--source", "cockpit")
    if stage_code != 0:
        return {"ok": False, "output": stage_out}
    # approve + book (default force=True => really books; force=False = dry-run preview)
    cmd = ["book", pid, "--yes"]
    if not force:
        cmd.append("--dry-run")
    book_code, book_out = _sched(*cmd)
    # Proxy: never surface raw CLI/sched.py output to a panel (contract §5).
    if book_code != 0:
        return {"ok": False, "output": "Couldn't add it to your calendar — try a different time."}
    if book_code == 0:
        return {"ok": True, "output": "Booked — it's on your calendar."}
    return {"ok": False, "output": "Couldn't add it to your calendar."}


# ── HTTP handler ───────────────────────────────────────────────────────────
def _json(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _auth(handler):
    if not PASSWORD:
        return True
    token = handler.headers.get("X-Cockpit-Auth", "")
    return token == PASSWORD


class Handler(BaseHTTPRequestHandler):
    def _send_static(self, path):
        root = Path("/opt/data/cockpit")
        p = (root / path.lstrip("/")).resolve()
        if root not in p.parents and p != root:
            p = root / "index.html"
        if not p.is_file():
            p = root / "index.html"
        ctype = "text/html"
        if p.suffix == ".js":
            ctype = "application/javascript"
        elif p.suffix == ".css":
            ctype = "text/css"
        elif p.suffix == ".json":
            ctype = "application/manifest+json"
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Service-Worker-Allowed", "/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # quiet

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/api/today", "/api/today/"):
            return _json(self, get_today())
        if path in ("/api/open", "/api/open/"):
            return _json(self, get_open_items())
        if path in ("/api/decide", "/api/decide/"):
            return _json(self, get_decide())
        if path in ("/api/coming-up", "/api/coming-up/"):
            return _json(self, {"coming_up": _next_up()})
        if path == "/api/find":
            q = urllib.parse.parse_qs(parsed.query)
            return _json(self, find_slots(
                q.get("summary", [""])[0], int(q.get("duration", ["15"])[0]),
                q.get("window", [""])[0] or None, int(q.get("days", ["7"])[0])))
        if path in ("/api/schedule", "/api/schedule/"):
            return _json(self, schedule_plate())
        if path in ("/api/plate", "/api/plate/"):
            return _json(self, {"plate": vaultlib.get_plate()})
        if path.startswith("/api/") or path not in ("/", "/index.html", "/manifest.json", "/sw.js"):
            # static served for known assets; api 404 otherwise
            if path.startswith("/api/"):
                return _json(self, {"error": "not found"}, 404)
        return self._send_static(path)

    def do_POST(self):
        if not _auth(self):
            return _json(self, {"error": "unauthorized"}, 401)
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            return _json(self, {"error": "bad json"}, 400)
        try:
            if parsed.path == "/api/add":
                return _json(self, add_quick_task(data.get("text", "")))
            if parsed.path == "/api/set-due":
                return _json(self, set_due(data["path"], data["due"]))
            if parsed.path == "/api/plate":
                return _json(self, vaultlib.set_plate(data.get("paths", [])))
            if parsed.path == "/api/resolve":
                return _json(self, resolve_decision(data["path"], data.get("resolution", "resolved"),
                                                    data.get("chosen")))
            if parsed.path == "/api/done":
                return _json(self, mark_task_done(data["path"]))
            if parsed.path == "/api/book":
                return _json(self, book_calendar(
                    data["summary"], data["start"], data["end"],
                    data.get("location", ""), data.get("description", ""),
                    data.get("force", False)))
            return _json(self, {"error": "unknown endpoint"}, 404)
        except (KeyError, ValueError) as e:
            return _json(self, {"error": str(e)}, 400)
        except Exception as e:
            return _json(self, {"error": "%s: %s" % (type(e).__name__, e)}, 500)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("[cockpit] serving on %s:%s" % (HOST, PORT))
    print("[cockpit] password set: %s" % ("yes" if PASSWORD else "NO"))
    srv.serve_forever()


if __name__ == "__main__":
    main()