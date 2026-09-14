#!/usr/bin/env python3
"""
Cockpit Phase-2 selftest — Hephaestus, 2026-09-14.

Boots cockpit_api on a test port against a TEMP vault clone (never the real
vault), then exercises the HTTP endpoints via urllib: today, open-items, and a
frontmatter-safe write (+ safety refusal). Also confirms the PWA static files
exist and serve.

Run: /opt/hermes/.venv/bin/python /opt/data/vault-mcp/cockpit_selftest.py
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

import vaultlib  # core for fixture setup

# --- isolated temp vault ---
tmp = tempfile.mkdtemp(prefix="cockpit-selftest-")
root = Path(tmp)
(root / "00 Daily Scratchpad").mkdir(parents=True)
(root / "01 Inbox").mkdir(parents=True)
(root / "02 Planner").mkdir(parents=True)

# today's scratchpad with 1 done + 2 open
(root / "00 Daily Scratchpad" / "2026-09-14.md").write_text(
    "---\ntype: daily\n---\n\n## ✅ Today\n- [x] done one\n- [ ] open two\n- [ ] open three\n\n## ✅ Done\n- [x] done one\n",
    encoding="utf-8")
# a decision awaiting joe
(root / "01 Inbox" / "decide-something.md").write_text(
    "---\ntype: inbox\ntitle: decide something\nawaiting: joe\ndecision: open\n---\n\n# body\n",
    encoding="utf-8")
# an open task
(root / "02 Planner" / "open-task.md").write_text(
    "---\ntype: task\ntitle: open task\nstatus: open\n---\n\n# body\n",
    encoding="utf-8")
# a not-achieved goal (must appear in the full scan)
goals = root / "04 Inner World" / "My Life" / "Goals"
goals.mkdir(parents=True)
(goals / "Some Goal.md").write_text(
    "---\ntype: goal\ntitle: Some Goal\nstatus: not-achieved\nprogress: 40%\n---\n\n# body\n",
    encoding="utf-8")

print("=" * 62)
print("Cockpit Phase-2 iteration selftest")
print("temp vault:", tmp)
print("=" * 62)

# --- boot the API on a test port pointing at the temp vault ---
port = 8799
env = {**os.environ, "VAULT_ROOT": tmp, "COCKPIT_HOST": "127.0.0.1",
       "COCKPIT_PORT": str(port), "COCKPIT_PASSWORD": "testpw"}
proc = subprocess.Popen(
    ["/opt/hermes/.venv/bin/python", "/opt/data/vault-mcp/cockpit_api.py"],
    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import time
for _ in range(30):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/open" % port, timeout=2)
        break
    except Exception:
        time.sleep(0.2)

B = "http://127.0.0.1:%d" % port
hdr = {"Content-Type": "application/json", "X-Cockpit-Auth": "testpw"}

def get(p):
    with urllib.request.urlopen(B + p, timeout=5) as r:
        return r.status, json.loads(r.read())

def post(p, body):
    req = urllib.request.Request(B + p, data=json.dumps(body).encode(), headers=hdr)
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, json.loads(r.read())

# 1) today
try:
    s, d = get("/api/today")
    # counter is deduped titles from today's done-log + checked daily boxes
    ok = d["done_count"] >= 1 and "done_titles" in d
    print("\n[PASS] /api/today deduped counter (done=%d) %r" % (d["done_count"], d["done_titles"]) if ok
          else "\n[FAIL] /api/today: %r" % d)
except Exception as e:
    print("\n[FAIL] /api/today: %s: %s" % (type(e).__name__, e))

# 2) open items — tasks ONLY (proxied plain-English; goals/daily/decisions excluded)
try:
    s, d = get("/api/open")
    items = d.get("items", [])
    kinds = sorted(x.get("kind") for x in items)
    # pick list is tasks-only, proxied with plain labels; no internal tokens
    ok = len(items) >= 1 and set(kinds) <= {"task"} and \
        all("path" in x and "tag" in x and "on_plate" in x for x in items) and \
        not any("decide" in x.get("kind","") for x in items)
    print("\n[PASS] /api/open tasks-only proxied (n=%d, kinds=%s, plate=%s)" %
          (len(items), kinds, d.get("plate")) if ok
          else "\n[FAIL] /api/open tasks-only: n=%d kinds=%s items=%r" % (len(items), kinds, items[:2]))
except Exception as e:
    print("\n[FAIL] /api/open: %s: %s" % (type(e).__name__, e))

# 2a) HARD RULE: no developer-crud in ANY read payload (contract §5)
try:
    s, d = get("/api/open")
    # path is machine-only (write handle), never rendered — strip it from audit
    blob = json.dumps(d)
    blob = json.dumps({k: v for k, v in d.items() if k != "plate"})
    plate_paths = " ".join(d.get("plate", []))
    st = json.dumps([{k: x.get(k) for k in x if k != "path"} for x in d.get("items", [])])
    blob = st + " " + plate_paths
    s2, d2 = get("/api/decide")
    blob += json.dumps([{k: x.get(k) for k in x if k != "path"} for x in d2.get("decisions", [])])
    blob += json.dumps(d2.get("coming_up", []))
    leaks = [t for t in ["sched.py", "vaultlib", ".md", "status:", "awaiting:",
                         "do-by:", "default:", "decision:", "WRITABLE_KEYS"]
             if t in blob]
    print("\n[PASS] no developer-crud in payloads (leaks=%s)" % (leaks or "none") if not leaks
          else "\n[FAIL] developer-crud leaked: %s" % leaks)
except Exception as e:
    print("\n[FAIL] no-crud audit: %s: %s" % (type(e).__name__, e))

# 2b) DECIDE panel: live decisions + cleared flag + coming-up (state design)
try:
    s, d = get("/api/decide")
    ok = "cleared" in d and "decisions" in d and "coming_up" in d
    ok2 = d["cleared"] is False and len(d["decisions"]) >= 1  # fixture has a decision
    print("\n[PASS] /api/decide (cleared=%s, decisions=%d, coming_up=%d)" %
          (d["cleared"], len(d["decisions"]), len(d["coming_up"])) if (ok and ok2)
          else "\n[FAIL] /api/decide: %r" % d)
    for dc in d["decisions"]:
        print("  •", dc.get("title"), "| lean:", dc.get("has_lean"),
              "| decide_by:", dc.get("has_decide_by"), "| options:", len(dc.get("options", [])))
except Exception as e:
    print("\n[FAIL] /api/decide: %s: %s" % (type(e).__name__, e))

# 2b-R5) DECIDE PURPOSE (round-5): body-parsed 3 options + lean + resolve-with-chosen
#  fixture: a decision carrying the caveman-native 1./2./3. list + (Lean)/Default.
try:
    (Path(tmp) / "01 Inbox" / "r5-decision.md").write_text(
        "---\ntype: inbox\ntitle: r5 decide that\nowner: hermes\nawaiting: joe\ndecision: open\ndue: \"2026-09-15\"\n---\n\n1. (Lean) Zero-setup win, 5 min\n2. Run the 60/40 lever on a real choice\n3. Do nothing this week\n\n**Default if no choice by 2026-09-15:** option 1\n",
        encoding="utf-8")
    s, d = get("/api/decide")
    r5 = next((x for x in d["decisions"] if x["path"].endswith("r5-decision.md")), None)
    ok = r5 is not None and len(r5["options"]) == 3 and r5["has_lean"] and r5["options"][0] == r5["lean"]
    print("\n[PASS] /api/decide body-parses 3 options + lean (opts=%d, lean=%r)" %
          (len(r5["options"]), r5["lean"]) if (ok and r5)
          else "\n[FAIL] decide body-parse: %r" % (r5 or "not found"))
    # resolve WITH chosen -> closes the decision and logs the pick (sanctioned),
    # WITHOUT adding a frontmatter `default` field (per Vesta/team convention).
    s, rr = post("/api/resolve", {"path": r5["path"], "chosen": r5["options"][2]})
    fm,_ = vaultlib.split_frontmatter((Path(tmp) / "01 Inbox" / "r5-decision.md").read_text())
    okr = fm.get("decision") == "resolved" and fm.get("awaiting") == "none" \
        and "default" not in fm and rr.get("logged") == "done-logged"
    # pick recorded in the cumulative done log (observable, no invented field)
    logs = ""
    lp = Path(tmp) / "05 Assets" / "Data" / "done" / "completions.jsonl"
    if lp.is_file():
        import json as _js
        logs = "\n".join(_js.loads(l)["task"] for l in lp.read_text().splitlines() if l.strip())
    logged_pick = "Do nothing this week" in logs
    print("\n[PASS] resolve-with-chosen closes+logs (no default field, logged=%s)" % logged_pick
          if (okr and logged_pick)
          else "\n[FAIL] resolve-chosen: fm=%r logged=%r rr=%r" % (fm, logged_pick, rr))
except Exception as e:
    print("\n[FAIL] decide round-5: %s: %s" % (type(e).__name__, e))

# 2b2) coming-up preview endpoint present + structured
try:
    s, d = get("/api/coming-up")
    ok = isinstance(d.get("coming_up"), list)
    print("\n[PASS] /api/coming-up (%d items)" % len(d.get("coming_up", [])) if ok
          else "\n[FAIL] /api/coming-up: %r" % d)
except Exception as e:
    print("\n[FAIL] /api/coming-up: %s: %s" % (type(e).__name__, e))

# 2b) ACTIVE PLATE write + get (vault-canonical, distinct state)
try:
    s, d = post("/api/plate", {"paths": ["02 Planner/open-task.md", "01 Inbox/decide-something.md"]})
    ok = d.get("plate") == ["02 Planner/open-task.md", "01 Inbox/decide-something.md"]
    s2, d2 = get("/api/plate")
    ok2 = d2.get("plate") == d.get("plate")
    print("\n[PASS] /api/plate write+read (2 on plate)" if (ok and ok2)
          else "\n[FAIL] /api/plate: %r / %r" % (d, d2))
except Exception as e:
    print("\n[FAIL] /api/plate: %s: %s" % (type(e).__name__, e))

# 2c) scheduler AUTOFILL from picked tasks (calls sched find — may be network;
# assert it returns a per-plate-item structure regardless of find result)
try:
    s, d = get("/api/schedule")
    ok = d.get("plate_count", 0) >= 0 and isinstance(d.get("items"), list)
    # round-2: items carry structured `slots`, never raw output/CLI strings
    clean = all("output" not in it and "skipped" not in it for it in d.get("items", []))
    print("\n[PASS] /api/schedule clean slots (plate_count=%d, items=%d, no-CLI=%s)" %
          (d.get("plate_count"), len(d.get("items", [])), clean) if (ok and clean)
          else "\n[FAIL] /api/schedule: %r" % d)
    for it in d.get("items", []):
        print("  •", it.get("title"), "| slots:", len(it.get("slots", [])))
except Exception as e:
    print("\n[FAIL] /api/schedule: %s: %s" % (type(e).__name__, e))

# 3) resolve a decision (safe write)
try:
    s, d = post("/api/resolve", {"path": "01 Inbox/decide-something.md"})
    fm, _ = vaultlib.split_frontmatter((root / "01 Inbox/decide-something.md").read_text())
    ok = fm.get("decision") == "resolved" and fm.get("awaiting") == "none"
    print("\n[PASS] /api/resolve (decision->resolved)" if ok else "\n[FAIL] /api/resolve: %r %r" % (d, fm))
except Exception as e:
    print("\n[FAIL] /api/resolve: %s: %s" % (type(e).__name__, e))

# 3) WRITE-BACK (round-3): /api/done flips status open->done AND logs to /done
#    (daily ✅ Done + completions.jsonl), deduped so it's never double-counted.
# fixture: a fresh open task to complete
(Path(tmp) / "02 Planner" / "writeback-task.md").write_text(
    "---\ntype: task\ntitle: writeback task\nstatus: open\n---\n\n# body\n",
    encoding="utf-8")
try:
    s, d = post("/api/done", {"path": "02 Planner/writeback-task.md"})
    # 1) status flipped open->done in the note
    fm, _ = vaultlib.split_frontmatter((Path(tmp) / "02 Planner/writeback-task.md").read_text())
    flipped = fm.get("status") == "done"
    # 2) logged to cumulative done log (completions.jsonl in temp vault)
    logp = Path(tmp) / "05 Assets" / "Data" / "done" / "completions.jsonl"
    logged = logp.is_file() and '"writeback task"' in logp.read_text()
    # 3) trace returned (observable read-back)
    traced = d.get("ok") and d.get("logged_log") and d.get("status_after") == "done"
    # 4) DEDUPE: hitting done again is a no-op (no second log line)
    s2, d2 = post("/api/done", {"path": "02 Planner/writeback-task.md"})
    deduped = d2.get("deduped") is True
    lines_after = logp.read_text().count("writeback task") if logp.is_file() else 0
    print("\n[PASS] WRITE-BACK: flipped=%s logged=%s traced=%s deduped=%s (log lines=%d)" %
          (flipped, logged, traced, deduped, lines_after) if (flipped and logged and traced and deduped and lines_after == 1)
          else "\n[FAIL] write-back: flipped=%s logged=%s traced=%s deduped=%s lines=%d resp=%r" %
               (flipped, logged, traced, deduped, lines_after, d))
except Exception as e:
    print("\n[FAIL] write-back: %s: %s" % (type(e).__name__, e))

# 3b) /api/today counts the completed writeback task (deduped, not 2x)
try:
    s, d = get("/api/today")
    ok = d["done_count"] >= 2 and d["done_titles"].count("writeback task") == 1
    print("\n[PASS] counter dedupes write-back (done=%d, wb x%d)" %
          (d["done_count"], d["done_titles"].count("writeback task")) if ok
          else "\n[FAIL] counter dedupe: %r" % d)
except Exception as e:
    print("\n[FAIL] counter dedupe: %s: %s" % (type(e).__name__, e))

# 3c) DONE STAYS IN PLACE + filtered out of /api/open (Joe done-decision)
#   completing does NOT move the note; the scan must exclude status:done so it
#   is never re-pulled. Verify the writeback task (now done) is NOT in /api/open.
try:
    s, d = get("/api/open")
    pulled = [x["title"] for x in d["items"] if x["title"] == "writeback task"]
    note_exists = (Path(tmp) / "02 Planner" / "writeback-task.md").is_file()
    ok = len(pulled) == 0 and note_exists  # in place (not moved) AND not re-pulled
    print("\n[PASS] done stays-in-place + filtered (exists=%s, re-pulled=%d)" %
          (note_exists, len(pulled)) if ok
          else "\n[FAIL] done-filter: exists=%s re-pulled=%r" % (note_exists, pulled))
except Exception as e:
    print("\n[FAIL] done-filter: %s: %s" % (type(e).__name__, e))

# 3d) ROUND-4 ROOT-CAUSE: frontmatter writes are UNQUOTED for plain scalars
#   (`status: done`, `type: task`) — the old `status: "done"` broke the Vesta/
#   validate-vault scanner (it reads literal quotes). Verify the write-back
#   note now has `status: done` (no quotes) AND round-trips through the vaultlib
#   split/parse unchanged.
try:
    text = (Path(tmp) / "02 Planner" / "writeback-task.md").read_text(encoding="utf-8")
    fm_lines = text.split("---")[1]
    quoted = [l for l in fm_lines.splitlines() if l.strip().endswith('"')]
    # re-parse via vaultlib to prove round-trip (parses to string, not bool)
    fm2, _ = vaultlib.split_frontmatter(text)
    ok = "status: done" in fm_lines and "status: \"done\"" not in fm_lines \
        and fm2.get("status") == "done" and fm2.get("type") == "task"
    print("\n[PASS] frontmatter plain-scalar unquoted + round-trip (status=%r)" %
          (fm2.get("status"),) if ok
          else "\n[FAIL] frontmatter quoting: lines=%r parsed=%r" % (fm_lines.splitlines(), fm2))
except Exception as e:
    print("\n[FAIL] frontmatter round-trip: %s: %s" % (type(e).__name__, e))

# 3e) ROUND-4: scheduler dynamic + conflict-aware (no past / spread / not all 07:00)
#   PATCH sched.py PROPOSAL_DIR to a temp dir so it can write, then run cmd_find.
try:
    import scripts.sched as _sched, tempfile as _tf, argparse as _ap
    _sched.PROPOSAL_DIR = Path(_tf.mkdtemp(prefix="sched-round4-"))
    _a = _ap.Namespace(summary="round4 test", duration=30, window=None,
                       days=5, date_from=None, count=3)
    _sched.cmd_find(_a)
    # read back the staged candidate slots
    import glob as _g, json as _js
    sp = _g.glob(str(_sched.PROPOSAL_DIR / ".find-*.json"))[0]
    cands = _js.loads(Path(sp).read_text())["candidates"]
    from datetime import datetime as _D2
    from zoneinfo import ZoneInfo as _Z2
    _NOW = _D2.now(_Z2("Europe/Rome"))
    # (a) no past slot today
    no_past = all(not(c["date"] == _NOW.strftime("%Y-%m-%d") and c["start"] < _NOW.strftime("%H:%M"))
                  for c in cands)
    # (b) spread: not all same start time
    starts = [c["start"] for c in cands]
    spread = len(set(starts)) > 1
    # (c) distinct days
    distinct = len(set(c["date"] for c in cands)) == len(cands)
    print("\n[PASS] scheduler no-past + spread + distinct-days (dates=%s starts=%s)" %
          ([c["date"] for c in cands], starts) if (no_past and spread and distinct)
          else "\n[FAIL] scheduler: no_past=%s spread=%s distinct=%s cands=%r" %
               (no_past, spread, distinct, cands))
except Exception as e:
    print("\n[FAIL] scheduler round4: %s: %s" % (type(e).__name__, e))

# 3c) ROUND-7 QUICK-ADD: POST /api/add persists a NEW open-task note to the
#   Planner source (vault-canonical), which /api/open then surfaces.
try:
    s, d = post("/api/add", {"text": "Quick added round7 test task"})
    ok_path = d.get("ok") is True and d.get("path","").startswith("02 Planner/Tasks/open/")
    # the note now exists on disk with the checkbox body + status open
    new_p = Path(tmp) / d["path"]
    txt = new_p.read_text(encoding="utf-8") if new_p.exists() else ""
    fm_new,_ = vaultlib.split_frontmatter(txt)
    checkbox = "- [ ] Quick added round7 test task" in txt
    ok_note = fm_new.get("type") == "task" and fm_new.get("status") == "open" and checkbox
    # and /api/open now lists it (created eligible task, not filtered as done)
    s2, d2 = get("/api/open")
    in_open = any(x.get("path") == d["path"] for x in d2["items"])
    print("\n[PASS] quick-add persists + shows in open (path=%s, checkbox=%s, in_open=%s)" %
          (d.get("path"), checkbox, in_open) if (ok_path and ok_note and in_open)
          else "\n[FAIL] quick-add: ok=%s note=%s in_open=%s d=%r" % (ok_path, ok_note, in_open, d))
except Exception as e:
    print("\n[FAIL] quick-add round-trip: %s: %s" % (type(e).__name__, e))

# 3d) ROUND-6 PICK DESELECT: a picked task can be REMOVED from the plate and
#   return to the open list (unpick without completing; frees a slot).
#   NOTE: once 02 Planner/Tasks/open/ exists, /api/open reads ONLY that dir
#   (planner source is authoritative) — so the fixture must live there.
try:
    ds_dir = Path(tmp) / "02 Planner" / "Tasks" / "open"; ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "deselect-task.md").write_text(
        "---\ntype: task\ntitle: deselect task\nstatus: open\n---\n\n- [ ] deselect task\n",
        encoding="utf-8")
    dp = "02 Planner/Tasks/open/deselect-task.md"
    s, d = post("/api/plate", {"paths": [dp]})
    s2, d2 = get("/api/open")
    picked = [x for x in d2["items"] if x["path"] == dp]
    on_plate = len(picked) == 1 and picked[0]["on_plate"] is True
    # deselect = write plate without it -> returns to open list, plate freed
    s3, d3 = post("/api/plate", {"paths": []})
    s4, d4 = get("/api/open")
    after = [x for x in d4["items"] if x["path"] == dp]
    back_in_list = len(after) == 1 and after[0]["on_plate"] is False and d4["plate"] == []
    # task is NOT done (deselect never marks complete)
    fm, _ = vaultlib.split_frontmatter((ds_dir / "deselect-task.md").read_text())
    not_done = fm.get("status") != "done"
    print("\n[PASS] pick+deselect round-trip (on_plate=%s, back-in-list=%s, not-done=%s)" %
          (on_plate, back_in_list, not_done) if (on_plate and back_in_list and not_done)
          else "\n[FAIL] deselect: on_plate=%s back=%s not_done=%s" %
               (on_plate, back_in_list, not_done))
except Exception as e:
    print("\n[FAIL] deselect: %s: %s" % (type(e).__name__, e))

# 4) resolve only updates frontmatter — body untouched (Original Text safe)
try:
    fm, body = vaultlib.split_frontmatter((root / "01 Inbox/decide-something.md").read_text())
    ok = "# body" in body and "awaiting" in fm
    print("\n[PASS] resolve left body intact (Original-Text safe)" if ok else "\n[FAIL] body mutated")
except Exception as e:
    print("\n[FAIL] body-check: %s: %s" % (type(e).__name__, e))

# 5) unauthenticated write refused
try:
    req = urllib.request.Request(B + "/api/resolve", data=json.dumps({"path": "x"}).encode(),
                                 headers={"Content-Type": "application/json"})  # no auth
    urllib.request.urlopen(req, timeout=5)
    print("\n[FAIL] auth (write allowed w/o password!)")
except urllib.error.HTTPError as e:
    print("\n[PASS] auth (write w/o password refused, %s)" % e.code)

# 6) static PWA files serve
try:
    root_html = urllib.request.urlopen(B + "/", timeout=5).read().decode()
    ok = "Joe's Cockpit" in root_html
    m = urllib.request.urlopen(B + "/manifest.json", timeout=5).status
    print("\n[PASS] static PWA serves (index + manifest %s)" % m if ok else "\n[FAIL] static")
except Exception as e:
    print("\n[FAIL] static: %s: %s" % (type(e).__name__, e))

proc.terminate()
try:
    proc.wait(timeout=5)
except Exception:
    proc.kill()
print("\n[done] temp vault removed:", root.exists() is False)
print("(kept tmp dir for inspection:", tmp, ")")