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
import threading
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time as _time

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
AUTOCOMMIT = "/opt/data/scripts/vault-autocommit.sh"
DONE_LOG = VAULT / "05 Assets" / "Data" / "done" / "completions.jsonl"

# ── ROUND-15: auto git commit+push after each cockpit write-back ─────────────
# Joe's rule: every quick-add / decider move / scheduler book syncs the vault
# repo (mobile + container + GitHub) so nothing is ever only-in-one-place. This
# is the COCKPIT's OWN write path — agents still go through the Vesta commit
# gate (vault-commit.sh); this never touches that path. Mechanics:
#   - Scoped: only the files the write just addressed (task note / plate / done
#     log / decision note) are committed — never `git add -A` transient junk.
#   - NON-BLOCKING: the UI has already rendered; we fire in a background thread
#     so a tap is NEVER delayed waiting on a commit/push.
#   - COALESCED: a ~2s debounce collapses a burst of actions into ONE commit.
#   - Idempotent: vault-autocommit.sh no-ops cleanly when there's nothing new.
AUTOCOMMIT_DEBOUNCE = 2.0
_autocommit_lock = threading.Lock()
_autocommit_timer = None
_pending_paths = set()          # vault-relative paths staged for the next commit
_pending_msgs = []              # human short notes for the commit subject

def _vault_rel(path) -> str:
    """Normalize an absolute or already-relative vault path to repo-relative."""
    p = Path(str(path))
    if p.is_absolute():
        try:
            p = p.relative_to(VAULT)
        except ValueError:
            return None
    return p.as_posix()

def _run_autocommit():
    """Drain the pending queue and fire ONE scoped commit+push in the background."""
    global _autocommit_timer
    with _autocommit_lock:
        paths = sorted(_pending_paths)
        msgs = _pending_msgs[:]
        _pending_paths.clear()
        _pending_msgs.clear()
        _autocommit_timer = None
    if not paths:
        return
    subject = msgs[-1] if msgs else "Cockpit sync"
    # vault-autocommit.sh only touches the explicit paths; no Vesta gate here.
    subprocess.Popen(
        [AUTOCOMMIT, subject, *paths],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def queue_vault_commit(rel_path, message):
    """Register a vault file this write changed and schedule a coalesced push."""
    rel = _vault_rel(rel_path)
    if not rel:
        return
    global _autocommit_timer
    with _autocommit_lock:
        if rel.endswith(".md") or rel.endswith(".jsonl"):
            _pending_paths.add(rel)
            if message and (not _pending_msgs or _pending_msgs[-1] != message):
                _pending_msgs.append(message)
        if _autocommit_timer is None:
            _autocommit_timer = threading.Timer(AUTOCOMMIT_DEBOUNCE, _run_autocommit)
            _autocommit_timer.daemon = True
            _autocommit_timer.start()

def _today():
    """Resolve today's date per-request (was a frozen module constant — the
    long-lived container answered stale dates past midnight)."""
    return date.today().isoformat()


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
                if rec.get("date") != _today():
                    continue
                t = (rec.get("task") or "").strip()
                if not t:
                    continue
                seen.setdefault(t.lower(), t)
    except OSError:
        pass
    # source 2: today's daily checked Done boxes
    try:
        p = VAULT / "00 Daily Scratchpad" / ("%s.md" % _today())
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
    return {"date": _today(), "done_count": len(done), "done_titles": done}


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
    r = vaultlib.set_frontmatter(path, {"due": due_date})
    queue_vault_commit(r.get("path", path), "Cockpit: set due on a task")
    return r


def add_quick_task(text):
    """Quick-add a task to the Planner open source (round-7). Vault-canonical
    via vaultlib.create_open_task (new open-task note, GL-002, Original-Text-
    safe — never touches existing notes/bodies)."""
    try:
        r = vaultlib.create_open_task(text)
        if r.get("ok"):
            queue_vault_commit(r["path"], "Cockpit: quick add – %s" % r.get("title", "task"))
        return r
    except ValueError as e:
        return {"ok": False, "output": str(e)}


def add_inbox_capture(text, awaiting="hermes"):
    """Quick-capture a line to the INBOX (Omega P1), for the BOTS to file+act.

    Vault-canonical via vaultlib.create_inbox_capture — a NEW 01 Inbox capture
    note (type:inbox / awaiting:hermes default / decision:open, GL-002 +
    Original-Text-safe). awaiting:hermes routes it to Hermes' processing lane
    (NEVER Joe's Waiting-on-You); pass awaiting="joe" to override the lane.
    Same safe-write path as create_open_task, but lands in the Inbox instead
    of the Planner open-task dir.
    """
    try:
        r = vaultlib.create_inbox_capture(text, awaiting=awaiting)
        if r.get("ok"):
            queue_vault_commit(r["path"], "Cockpit: captured to inbox – %s" % r.get("title", "capture"))
        return r
    except ValueError as e:
        return {"ok": False, "output": str(e)}


def rename_task(path, new_title):
    """Inline-rename a task (round-12). Original-Text-safe via
    vaultlib.rename_task: updates title + H1 + checkbox line only, never body
    prose. Returns the write trace so the UI can reflect it."""
    try:
        r = vaultlib.rename_task(path, new_title)
        queue_vault_commit(path, "Cockpit: renamed task")
        return {"ok": True, "path": r["path"], "old": r["old"],
                "title": r["title"], "output": "Renamed ✔"}
    except (ValueError, FileNotFoundError) as e:
        return {"ok": False, "output": "Couldn't rename that task."}


def revert_done(path):
    """Undo a completion (round-12): reopen the task + remove its /done log.

    What Undo does: flips the note status done->open (via vaultlib), removes
    the matching completion row from the cumulative completions.jsonl, and
    strips the matching `- HH:MM — <title>` line from today's daily ✅ Done,
    so the task returns to the open list and the counter drops back by one.
    """
    out = {"ok": False, "path": path}
    try:
        note = vaultlib.read_note(path)
    except Exception:
        return {**out, "output": "Couldn't find that task to undo."}
    title = (note.get("frontmatter", {}).get("title") or "").strip()
    before = str(note.get("frontmatter", {}).get("status") or "").lower()
    if before != "done":
        return {**out, "output": "That task isn't marked done.", "not_done": True}
    # 1) reopen the note
    try:
        vaultlib.set_frontmatter(path, {"status": "open"})
    except Exception:
        return {**out, "output": "Couldn't reopen the task."}
    removed_lines, removed_daily = 0, False
    # 2) remove matching completion rows (exact title) from the cumulative log
    try:
        if DONE_LOG.is_file():
            lines = [l for l in DONE_LOG.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            keep = []
            for l in lines:
                try:
                    rec = json.loads(l)
                    if rec.get("task") == title:
                        removed_lines += 1
                        continue
                except json.JSONDecodeError:
                    pass
                keep.append(l)
            DONE_LOG.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    except OSError:
        pass
    # 3) strip the matching daily ✅ Done line (best-effort, exact title match)
    try:
        daily = VAULT / "00 Daily Scratchpad" / ("%s.md" % _today())
        if daily.is_file():
            txt = daily.read_text(encoding="utf-8", errors="replace")
            kept = [l for l in txt.splitlines()
                    if not (l.strip().startswith("- ") and title.strip() and
                            ("— " + title) in l)]
            if len(kept) != len(txt.splitlines()):
                removed_daily = True
                daily.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        pass
    # round-15: auto-sync everything this undo touched (note + done-log records)
    queue_vault_commit(path, "Cockpit: undone a task")
    queue_vault_commit(DONE_LOG, "Cockpit: undone a task")
    try:
        queue_vault_commit("00 Daily Scratchpad/%s.md" % date.today().isoformat(), "Cockpit: undone a task")
    except Exception:
        pass
    return {**out, "ok": True, "output": "Undone — task reopened.",
            "removed_log_lines": removed_lines, "removed_daily": removed_daily,
            "title": title}


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
    # round-15: auto-sync the decision note + the done-log records (when logged)
    queue_vault_commit(path, "Cockpit: resolved a decision")
    if logged_log:
        today_note = "00 Daily Scratchpad/%s.md" % date.today().isoformat()
        queue_vault_commit(today_note, "Cockpit: decision logged")
        queue_vault_commit("05 Assets/Data/done/completions.jsonl", "Cockpit: decision logged")
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

    # round-15: auto-sync the flipped note + the done-log records it touched
    queue_vault_commit(write.get("path", path), "Cockpit: task done – %s" % title[:60])
    if logged_to and not logged_to.startswith("("):
        queue_vault_commit(logged_to, "Cockpit: done logged")
    if log_path:
        queue_vault_commit(log_path, "Cockpit: done logged")

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
    # ROUND-18: load any already-booked events so Plan can LOCK those cards.
    booked = _booked_proposals()
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
        item = {
            "idx": idx,              # stable index for the per-row manual override
            "path": path,            # round-18: needed for inline edit rename
            "title": title,
            "slots": slots[:3],  # a few clear choices, not a dump
            "found": len(slots) > 0,
            "locked": False, "booked_label": "",
        }
        # If this task already has a booked event, LOCK the card: hide the slot
        # options + Book it, show the "Booked for …" badge instead.
        if title in booked:
            b = booked[title]
            dt, st = (b.get("date") or ""), b.get("start") or ""
            item["locked"] = True
            item["event_id"] = b.get("event_id", "")
            # full ISO start/end so the inline reschedule picker can prefill
            item["booked_start"] = b.get("start", "")
            item["booked_end"] = b.get("end", "")
            # human badge: "09-15 07:00"
            item["booked_label"] = "%s %s" % (dt[5:10] if len(dt) >= 10 else dt, st[11:16] if len(st) >= 16 else st)
        out["items"].append(item)
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


# ── Round-18: locked/scheduled state + real-time gcal patch ────────────────
PROPOSAL_DIR = Path("/opt/data/cockpit/scheduling-proposals")


def _booked_proposals() -> dict:
    """Map task-title -> booked proposal for every `status: booked` proposal.

    Booked events carry the vault task title in `title:` + a real Google
    `event_id:` + date/start/end. We key by normalized title so Plan can lock
    the exact plate card that already has a calendar event.
    """
    out = {}
    if not PROPOSAL_DIR.is_dir():
        return out
    for f in PROPOSAL_DIR.glob("*.md"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^---\n(.*?)\n---", text, re.S)
            if not m:
                continue
            meta = {}
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = json.loads(v.strip())
            if str(meta.get("status", "")).lower() == "booked" and meta.get("event_id"):
                title = (meta.get("title") or f.stem).strip()
                if title:
                    out[title] = meta
        except Exception:
            continue
    return out


def update_calendar_event(summary, new_title, event_id, start_iso="", end_iso=""):
    """PATCH an existing Google Calendar event (round-18 rename + round-19 move).

    Same creds/timezone handling as the round-15 create fix (HERMES_HOME + the
    resolved venv python). Builds the patch from whatever is provided:
      - new_title  -> template the title (rename)
      - start/end  -> move the slot (reschedule), Europe/Rome applied in gapi.
    On success rewrites the matching `status: booked` proposal file (title
    and/or date/start/end) so the Plan lock + booked badge stay in sync.
    Returns a clean, proxied result — no raw CLI to the panel.
    """
    gapi = "/opt/data/skills/productivity/google-workspace/scripts/google_api.py"
    py = _resolve_google_py()
    cmd = [py, gapi, "calendar", "update", event_id]
    if new_title:
        cmd += ["--summary", new_title]
    if start_iso:
        cmd += ["--start", start_iso]
    if end_iso:
        cmd += ["--end", end_iso]
    r = subprocess.run(cmd, capture_output=True, text=True, env=_sched_env(), timeout=60)
    if r.returncode != 0:
        return {"ok": False,
                "output": "Couldn't update the calendar event — the Google service didn't confirm it."}
    try:
        created = json.loads(r.stdout.strip())
        if created.get("status") == "updated" and created.get("id"):
            # keep the title-keyed Plan lock + booked badge in sync: update them
            # (title and/or slot) in the matching booked proposal
            for f in (PROPOSAL_DIR.glob("*.md") if PROPOSAL_DIR.is_dir() else []):
                try:
                    txt = f.read_text(encoding="utf-8", errors="replace")
                    m = re.search(r"^---\n(.*?)\n---", txt, re.S)
                    if not m:
                        continue
                    meta = {}
                    for line in m.group(1).splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            try:
                                meta[k.strip()] = json.loads(v.strip())
                            except Exception:
                                meta[k.strip()] = v.strip()
                    if str(meta.get("status", "")).lower() == "booked" and meta.get("event_id") == event_id:
                        import json as _j
                        if new_title:
                            meta["title"] = new_title
                        if start_iso:
                            meta["date"] = start_iso[:10]
                            meta["start"] = start_iso
                        if end_iso:
                            meta["end"] = end_iso
                        fm = ["---"]
                        for k, v in meta.items():
                            fm.append("%s: %s" % (k, _j.dumps(v, ensure_ascii=False)))
                        fm.append("---")
                        rest = txt.split("---", 2)[2] if txt.count("---") >= 2 else ""
                        # keep the H1/# line in the body synced if it matched old title
                        if new_title and rest.lstrip().startswith("# "):
                            rest = re.sub(r"^# .*", "# %s" % new_title, rest, count=1)
                        f.write_text("\n".join(fm) + rest, encoding="utf-8")
                        break
                except Exception:
                    continue
            return {"ok": True, "event_id": created.get("id"),
                    "summary": created.get("summary"), "start": created.get("start", start_iso)}
    except Exception:
        pass
    return {"ok": False,
            "output": "Couldn't update the calendar event — the service didn't confirm it."}


def _resolve_google_py() -> str:
    """Which python has googleapiclient (same candidates sched.py uses)."""
    for p in ("/opt/data/.google-venv/bin/python",
              "/opt/data/.gapi-venv/bin/python",
              "/opt/data/.venv/bin/python"):
        if Path(p).exists():
            probe = subprocess.run([p, "-c", "import googleapiclient"],
                                   capture_output=True, text=True)
            if probe.returncode == 0:
                return p
    return sys.executable or "python3"


def _sched_env() -> dict:
    env = dict(os.environ)
    env["HERMES_HOME"] = "/opt/data"
    return env


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
    # A dry-run / preview is NEVER a book — it must not report success on a
    # Google event that doesn't exist yet. Only a forced book is "Booked".
    if not force:
        return {"ok": False, "preview": True,
                "output": "Preview only — not booked. A real book needs a confirmed tap."}
    # Proxy: never surface raw CLI/sched.py output to a panel (contract §5).
    # Only "Booked" when the book command truly succeeded — which now means
    # sched.py saw a real Google event (id + htmlLink), not just rc-clean.
    # FALSE SUCCESS was happening because a swallowed google 400 still printed
    # and returned rc 0; the fixed sched.py commits status:booked only on a
    # verified event. So honor book_code strictly and surface a real error.
    if book_code != 0:
        return {"ok": False, "output": "Couldn't add it to your calendar — the calendar service didn't confirm it. Try a different time."}
    return {"ok": True, "output": "Booked — it's on your calendar."}


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
        elif p.suffix == ".png":
            ctype = "image/png"
        elif p.suffix == ".svg":
            ctype = "image/svg+xml"
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Service-Worker-Allowed", "/")
        # never serve a stale app shell / service worker — a deploy must reach
        # the device immediately (was the pick-persist "stale HTML" root cause)
        if path in ("/", "/index.html", "/manifest.json", "/sw.js"):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Observability (omega pick-persist reopen): this was `pass # quiet` —
        # a blind spot that let 9 frontend-guessing fixes ship. Log every
        # request: METHOD path auth -> status, so a failing write is visible.
        try:
            meth = getattr(self, "command", "?")
            pth = getattr(self, "path", "?")
            code = ""
            if len(args) >= 2 and str(args[1]).isdigit():
                code = " -> " + str(args[1])
            auth = ""
            if meth == "POST":
                tok = self.headers.get("X-Cockpit-Auth", "") if hasattr(self, "headers") else ""
                auth = " auth=" + ("OK" if tok == PASSWORD else ("BAD" if tok else "MISSING"))
            sys.stderr.write("[cockpit-req] %s %s%s%s\n" % (meth, pth, auth, code))
        except Exception:
            pass

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
            if parsed.path == "/api/add-capture":
                return _json(self, add_inbox_capture(
                    data.get("text", ""), data.get("awaiting", "hermes")))
            if parsed.path == "/api/rename":
                return _json(self, rename_task(data["path"], data.get("title", data.get("new_title", ""))))
            if parsed.path == "/api/calendar/update":
                # round-18/19: PATCH an already-booked event after an inline
                # title edit (rename) OR a reschedule (move start/end). Rome.
                return _json(self, update_calendar_event(
                    data.get("summary", ""), data.get("new_title", data.get("title", "")),
                    data.get("event_id", ""),
                    data.get("start", ""), data.get("end", "")))
            if parsed.path == "/api/undone":
                return _json(self, revert_done(data["path"]))
            if parsed.path == "/api/set-due":
                return _json(self, set_due(data["path"], data["due"]))
            if parsed.path == "/api/plate":
                _sent = data.get("paths", [])
                sys.stderr.write("[cockpit-plate] POST paths=%r\n" % (_sent,))
                _plate_out = vaultlib.set_plate(_sent)
                sys.stderr.write("[cockpit-plate] -> persisted=%r\n" % (_plate_out.get("plate"),))
                queue_vault_commit("02 Planner/_ Active Plate.md", "Cockpit: plate updated")
                return _json(self, _plate_out)
            if parsed.path == "/api/resolve":
                return _json(self, resolve_decision(data["path"], data.get("resolution", "resolved"),
                                                    data.get("chosen")))
            if parsed.path == "/api/done":
                return _json(self, mark_task_done(data["path"]))
            if parsed.path == "/api/book":
                return _json(self, book_calendar(
                    data["summary"], data["start"], data["end"],
                    data.get("location", ""), data.get("description", ""),
                    # A book tap in the cockpit IS the approval (Joe's
                    # rule: pick a date -> on the calendar). Default force=True
                    # so it really books; a missing/False force used to take the
                    # --dry-run path and STILL report "Booked" while creating
                    # nothing (the device-only false-success). Preview is only
                    # surfaced when force is EXPLICITLY False AND honored as a
                    # preview below — never as success.
                    data.get("force", True)))
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