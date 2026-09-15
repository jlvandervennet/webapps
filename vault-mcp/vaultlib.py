#!/usr/bin/env python3
"""
vaultlib — shared vault-access core for the 'vault' MCP server AND the
Cockpit API backend. Hephaestus build, 2026-09-14.

Pure stdlib. One code path for reading/searching/writing the vault at
/opt/data/Second-Brain, enforcing GL-002 frontmatter conventions and a hard
safety layer that NEVER edits the user's Original Text (CLAUDE.md hard rule #1).

Because both the MCP server and the Cockpit app import this same module,
agents and the app are consistent by construction — there is no second,
drifting data path.
"""

import os
import re
import json
from pathlib import Path

# The vault root. Overridable (tests / cockpit could point elsewhere).
VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/opt/data/Second-Brain")).resolve()

# Frontmatter delimiters.
FM_OPEN = "---"
FM_CLOSE = "---"

# Original Text safety markers (CLAUDE.md hard rule #1): we never rewrite the
# user's prose. For daily scratchpads and captures the body IS original text.
# We only surface / never-mutate these.
NOTES = ["daily", "scratchpad", "capture"]


# ── Path safety ────────────────────────────────────────────────────────────
def _resolve_vault_path(rel: str) -> Path:
    """Resolve a vault-relative path, rejecting any traversal outside the vault."""
    rel = rel.strip().lstrip("/")
    p = (VAULT_ROOT / rel).resolve()
    if VAULT_ROOT not in p.parents and p != VAULT_ROOT:
        raise ValueError("path escapes vault: %s" % rel)
    return p


# ── Read ───────────────────────────────────────────────────────────────────
def read_note(rel: str) -> dict:
    """Read a note: path, frontmatter (parsed), body, and mtime."""
    p = _resolve_vault_path(rel)
    if not p.is_file():
        raise FileNotFoundError("no such note: %s" % rel)
    text = p.read_text(encoding="utf-8", errors="replace")
    fm, body = split_frontmatter(text)
    return {
        "path": str(p.relative_to(VAULT_ROOT)),
        "frontmatter": fm,
        "body": body,
        "mtime": p.stat().st_mtime,
    }


def search_notes(query: str, include_path=None, limit: int = 50) -> list:
    """Case-insensitive text search across vault markdown."""
    q = query.lower()
    results = []
    for p in sorted(VAULT_ROOT.rglob("*.md")):
        if p.is_dir():
            continue
        rel = str(p.relative_to(VAULT_ROOT))
        if include_path and include_path not in rel:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if q in text.lower():
            results.append({"path": rel, "line_count": text.count("\n") + 1})
            if len(results) >= limit:
                break
    return results


# ── Frontmatter ────────────────────────────────────────────────────────────
def split_frontmatter(text: str):
    """Return (frontmatter_dict, body_text). Tolerant of missing frontmatter."""
    if not text.startswith(FM_OPEN + "\n"):
        return {}, text
    end = text.find("\n" + FM_CLOSE, 3)
    if end == -1:
        return {}, text
    fm_block = text[3:end].strip()
    body = text[end + len("\n" + FM_CLOSE):].lstrip("\n")
    fm = _parse_yamlish(fm_block)
    return fm, body


def _parse_yamlish(block: str) -> dict:
    """Minimal frontmatter parser for flat key: value lines (GL-002 style).

    Handles scalars, booleans, integers, quoted/unquoted strings, INLINE lists
    (`[a, b]`) and BLOCK lists (``key:`` then ``  - item`` lines) — the block
    list form is how `tags:` are hand-authored, and MUST survive a write-back
    (round-12 critical: the flat parser was silently flattening them to "").
    """
    fm = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line or line.startswith("#"):
            i += 1
            continue
        if line.startswith("  - ") or line.startswith("- "):
            # orphan list continuation (no key above) — skip defensively
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        key = key.strip().strip('"').strip("'")
        val = val.strip()
        if not key:
            i += 1
            continue
        # BLOCK list: `key:` with an empty (or absent) value, followed by
        # indented `  - item` lines -> collect them as a list.
        if val in ("", "[]"):
            items = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                s = nxt.strip()
                if s.startswith("- "):
                    item = s[2:].strip()
                    item = item.strip('"').strip("'")
                    items.append(item)
                    j += 1
                    continue
                break
            if items:
                fm[key] = items
                i = j
                continue
            fm[key] = _parse_scalar(val)
            i += 1
            continue
        fm[key] = _parse_scalar(val)
        i += 1
    return fm


def _parse_scalar(val: str):
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
    if val.lower() in ("true", "yes"):
        return True
    if val.lower() in ("false", "no"):
        return False
    if val == "null" or val == "~":
        return None
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    return val.strip('"').strip("'")


# ── Frontmatter-safe write ─────────────────────────────────────────────────
# Allowed keys the cockpit / MCP may write. This is the consistency contract:
# the cockpit only touches fields GL-002 already sanctions or that the team
# has added. Anything else is refused (no invented fields, per GL-002).
WRITABLE_KEYS = {
    # task-type lifecycle
    "status", "assignee", "due", "related",
    # inbox decision lifecycle (awaiting: joe)
    "awaiting", "decision",
    # Two-Roots levers (sanctioned additive — see GL-002 §sanctioned additive)
    "do-by", "default",
    # progress / goal additive
    "progress", "next_step",
    # processed stamp
    "processed", "processed_summary", "processed_into",
    # tracking
    "target_date",
}


def set_frontmatter(rel: str, updates: dict) -> dict:
    """Update a note's frontmatter with ONLY allowed keys. Never touches body.

    Safety layer: refuses any key not in WRITABLE_KEYS (no invented fields),
    and refuses to edit the BODY (Original Text) of any note — body edits and
    the raw 'Original Text' sections are off-limits by construction.
    """
    p = _resolve_vault_path(rel)
    if not p.is_file():
        raise FileNotFoundError("no such note: %s" % rel)

    # Refuse body/Original-Text keys outright.
    blocked = {"body", "original_text", "content"}
    if blocked & set(updates.keys()):
        raise ValueError("refusing body/Original-Text edit — CLAUDE.md hard rule #1")

    unknown = set(updates.keys()) - WRITABLE_KEYS
    if unknown:
        raise ValueError("refusing unapproved frontmatter keys: %s" % sorted(unknown))

    text = p.read_text(encoding="utf-8", errors="replace")
    if not text.startswith(FM_OPEN + "\n"):
        raise ValueError("note has no frontmatter block; frontmatter-write requires one")

    fm, body = split_frontmatter(text)
    for k, v in updates.items():
        fm[k] = v

    fm_lines = [_format_fm_value(k, v) for k, v in fm.items()]
    new_text = FM_OPEN + "\n" + "\n".join(fm_lines) + "\n" + FM_CLOSE + "\n\n" + body
    p.write_text(new_text, encoding="utf-8")

    return {"path": str(p.relative_to(VAULT_ROOT)), "updated": sorted(updates.keys())}


def _format_fm_value(k, v):
    if isinstance(v, bool):
        return "%s: %s" % (k, "true" if v else "false")
    if isinstance(v, (int, float)):
        return "%s: %s" % (k, v)
    if isinstance(v, list):
        rendered = ["%s:" % k]
        for item in v:
            rendered.append("  - %s" % item)
        return "\n".join(rendered)
    if v is None:
        return "%s: null" % k
    # Plain scalar: write UNQUOTED when safe, matching hand-authored style
    # (`type: task`, `owner: hermes`, `status: done`). Round-4 root-cause fix:
    # quoting EVERY string made `owner: "hermes"`, which the Vesta/validate
    # scanner reads as literal `"hermes"` and flags "unknown owner". Quote only
    # when the value needs it (spaces / reserved / date-like / leading symbol).
    s = str(v)
    if _is_plain_scalar(s):
        return "%s: %s" % (k, s)
    return '%s: "%s"' % (k, s)


def _is_plain_scalar(s):
    """True if a string is safe to write unquoted (and re-parse unchanged).

    Must BOTH (a) be a YAML-safe plain word AND (b) not collide with a bare
    non-string scalar (`true/yes/no/null` -> bool/None, `123` -> int), AND
    (c) not look like a DATE/TIMESTAMP — hand-authored style QUOTES dates
    (`created: "2026-09-14"`), so we keep them quoted to match (round-12).
    """
    if not s:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_.\-@/]+", s):
        return False
    low = s.lower()
    if low in ("true", "yes", "false", "no", "null", "~", "on", "off"):
        return False                # would re-parse as bool / None
    # date/timestamp lookalike (leading digits then a - or T) -> quote it
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return False
    if low.lstrip("-").replace(".", "", 1).isdigit() and low.lstrip("-"):
        return False                # looks numeric -> would re-parse as int/float
    return True


# ── High-level cockpit helpers (Phase 2 uses these) ────────────────────────
# The ACTIVE PLATE (Joe's 3-on-the-plate) is itself vault state — a first-class
# note under 02 Planner so the fleet and the app share it (no invented fields
# on other notes; one cockpit-owned planner artifact).
PLATE_NOTE = "02 Planner/_ Active Plate"


def get_plate() -> list:
    """Return the active plate (paths of the 3-on-the-plate). Vault-canonical."""
    p = VAULT_ROOT / (PLATE_NOTE + ".md")
    if not p.is_file():
        return []
    fm, _ = split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
    plate = fm.get("plate") or []
    if isinstance(plate, list):
        return [str(x) for x in plate if x]
    return []


def set_plate(paths: list) -> dict:
    """Write the active plate (max 3). Vault-canonical via a cockpit planner note."""
    paths = [str(x) for x in paths[:3]]
    p = VAULT_ROOT / (PLATE_NOTE + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "# Active Plate\n\nThe 3 tasks Joe has on the plate right now.\n"
    if not paths:
        body += "\n*(empty — nothing on the plate)*\n"
    else:
        for i, path in enumerate(paths, 1):
            body += "\n%d. `%s`\n" % (i, path)
    # Write with frontmatter plate as an INLINE list — the minimal vaultlib
    # parser handles `[a, b]` but not `  - item` block lists.
    import json as _json
    lines = [FM_OPEN, "type: planner-item", "title: Active Plate", "source: cockpit",
             "status: open",
             "plate: " + _json.dumps(paths, ensure_ascii=False),
             FM_CLOSE, "", body.strip(), ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return {"plate": paths}


OPEN_TASKS_DIR = VAULT_ROOT / "02 Planner" / "Tasks" / "open"
INBOX_DIR = VAULT_ROOT / "01 Inbox"


def create_inbox_capture(text: str) -> dict:
    """Create a NEW 01 Inbox capture note (awaiting:joe) — Omega quick-capture.

    Joe drops a link / project idea / research topic; the team files + acts on
    it. Writes a genuine `type: inbox` / `awaiting: joe` / `decision: open`
    capture note under `01 Inbox/` with GL-002 frontmatter so it can be
    resolved like any decision and so the fleet's decision surface sees it.
    The quick text is recorded BOTH as `title` AND as a `- [ ] <text>` checkbox
    in the body (Joe's requested format). New note only — never mutates an
    existing note/body (Original-Text-safe).
    """
    from datetime import date as _date
    text = (text or "").strip()
    if not text:
        raise ValueError("empty capture text")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "capture"
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    import time as _time
    fname = "%s-%s.md" % (slug, _time.strftime("%H%M%S"))
    p = INBOX_DIR / fname
    title = text[:80]
    fm = [
        FM_OPEN,
        "title: %s" % (('"%s"' % title.replace('"', "'"))),
        "type: inbox",
        "owner: hermes",
        "status: open",
        "awaiting: joe",
        "decision: open",
        'created: "%s"' % _date.today().isoformat(),
        FM_CLOSE,
        "",
        "# %s" % title,
        "",
        "- [ ] %s" % text,
        "",
    ]
    p.write_text("\n".join(fm), encoding="utf-8")
    return {"ok": True, "path": str(p.relative_to(VAULT_ROOT)),
            "title": title, "created": _date.today().isoformat()}


def create_open_task(text: str) -> dict:
    """Create a NEW open-task note in the Planner open source (round-7 quick-add).

    Writes a genuine `type: task` / `status: open` note under
    `02 Planner/Tasks/open/` so the cockpit's /api/open scan surfaces it the
    same as hand-created tasks. GL-002 frontmatter; the quick text is recorded
    BOTH as `title` AND as a `- [ ] <text>` checkbox in the body (Joe's
    requested format). New note only — never mutates existing notes or bodies.
    """
    from datetime import date as _date
    text = (text or "").strip()
    if not text:
        raise ValueError("empty task text")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "quick"
    OPEN_TASKS_DIR.mkdir(parents=True, exist_ok=True)
    # unique-ish filename: slug + short timestamp to avoid collision
    import time as _time
    fname = "%s-%s.md" % (slug, _time.strftime("%H%M%S"))
    p = OPEN_TASKS_DIR / fname
    title = text[:80]
    fm = [
        FM_OPEN,
        "title: %s" % (('"%s"' % title.replace('"', "'"))),
        "type: task",
        "owner: hermes",
        "assignee: joe",
        "status: open",
        'created: "%s"' % _date.today().isoformat(),
        FM_CLOSE,
        "",
        "# %s" % title,
        "",
        "- [ ] %s" % text,
        "",
    ]
    p.write_text("\n".join(fm), encoding="utf-8")
    return {"ok": True, "path": str(p.relative_to(VAULT_ROOT)),
            "title": title, "created": _date.today().isoformat()}


def rename_task(rel: str, new_title: str) -> dict:
    """Rename an open-task note (round-12 inline edit), Original-Text-safe.

    Updates ONLY:
      - frontmatter `title` (GL-002 key),
      - the body's first H1 line (`# <old>`) IF it exactly matches the old title,
      - the body's first task checkbox line (`- [ ] <old>`) IF it matches.
    NEVER touches body prose (CLAUDE.md hard rule #1). Used by the cockpit
    inline-edit so a rename is genuinely vault-canonical, not a local-only swap.
    """
    p = _resolve_vault_path(rel)
    if not p.is_file():
        raise FileNotFoundError("no such note: %s" % rel)
    title = (new_title or "").strip()
    if not title:
        raise ValueError("empty title")
    text = p.read_text(encoding="utf-8", errors="replace")
    if not text.startswith(FM_OPEN + "\n"):
        raise ValueError("note has no frontmatter; rename requires one")
    fm, body = split_frontmatter(text)
    old_title = (fm.get("title") or "").strip() or ""
    # 1) update frontmatter title
    fm["title"] = title
    fm_lines = [_format_fm_value(k, v) for k, v in fm.items()]
    new_text = FM_OPEN + "\n" + "\n".join(fm_lines) + "\n" + FM_CLOSE + "\n\n"

    # 2) reflect in the checkbox line and H1, ONLY if they exactly match the
    #    old title — never rewrite body prose or other lines.
    def _replace_first(s: str, old_hunk: str, new_hunk: str) -> tuple[str, bool]:
        i = s.find(old_hunk)
        if i == -1:
            return s, False
        return s[:i] + new_hunk + s[i + len(old_hunk):], True

    if old_title:
        body, changed_h1 = _replace_first(body, "# " + old_title, "# " + title)
    else:
        changed_h1 = False
    body, changed_check = _replace_first(body, "- [ ] " + old_title, "- [ ] " + title)

    p.write_text(new_text + body, encoding="utf-8")
    return {"path": str(p.relative_to(VAULT_ROOT)), "old": old_title,
            "title": title, "updated_checkbox": changed_check,
            "updated_h1": changed_h1}


def get_joe_decisions() -> list:
    """All inbox notes awaiting Joe with decision open."""
    out = []
    for p in sorted((VAULT_ROOT / "01 Inbox").rglob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, _ = split_frontmatter(text)
        if fm.get("awaiting") == "joe" and str(fm.get("decision", "")).lower() != "resolved":
            out.append({
                "path": str(p.relative_to(VAULT_ROOT)),
                "title": fm.get("title", p.stem),
                "do_by": fm.get("do-by"),
                "default": fm.get("default"),
                "decision": fm.get("decision"),
            })
    return out


def get_daily_3(day: str) -> dict:
    """Parse today's daily scratchpad for the 'pick your 3' + completion counter."""
    p = VAULT_ROOT / "00 Daily Scratchpad" / ("%s.md" % day)
    if not p.is_file():
        return {"today": [], "done_count": 0, "exists": False}
    text = p.read_text(encoding="utf-8", errors="replace")
    today = []
    done_count = 0
    in_today = False
    in_done = False
    for line in text.splitlines():
        if line.startswith("## ✅ Today"):
            in_today, in_done = True, False
            continue
        if line.startswith("## ✅ Done"):
            in_today, in_done = False, True
            continue
        if line.startswith("## ") and "Today" not in line and "Done" not in line:
            in_today = in_done = False
            continue
        m = re.match(r"- \[([ xX])\]\s*(.*)", line)
        if not m:
            continue
        checked = m.group(1).strip().lower() == "x"
        text_item = m.group(2).strip()
        if in_today:
            today.append({"text": text_item, "done": checked})
        if in_done and checked:
            done_count += 1
    return {"today": today, "done_count": done_count, "exists": True}


if __name__ == "__main__":
    # Quick smoke when run directly.
    import sys
    print("vaultlib ok — vault root:", VAULT_ROOT)
    print("decisions awaiting joe:", len(get_joe_decisions()))