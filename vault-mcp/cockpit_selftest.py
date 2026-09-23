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
import urllib.parse
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
kanban_tmp = os.path.join(tmp, "test-kanban.db")
env = {**os.environ, "VAULT_ROOT": tmp, "COCKPIT_HOST": "127.0.0.1",
       "COCKPIT_PORT": str(port), "COCKPIT_PASSWORD": "testpw",
       # Ω v15: repoint the kanban subprocess at a throwaway engine DB so a
       # hermes-lane capture test never touches the live fleet board.
       "KANBAN_DB": kanban_tmp, "HERMES_HOME": "/opt/data"}
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

# ═══ 🏛 DECIDE-OVERHAUL (PRD t_ca2c7e4d, ratif. 2026-09-23) ═══
# context surface + edit-response: option-tap=confirm-as-is, "Other"=response:
#  fixture = a decision with BLUF lead, why-now section, source + related.
try:
    (Path(tmp) / "01 Inbox" / "oh-decision.md").write_text(
        "---\ntype: inbox\ntitle: approve the fueling plan\nowner: athena\nsource: \"[[Marathon Roadmap]]\"\nrelated: [Marathon Roadmap]\nawaiting: joe\ndecision: open\ndue: \"2026-09-30\"\n---\n\n# The fork (why it matters now)\nThe marathon is six weeks out so the fueling plan has to be settled before the long run.\n\n**Recommendation: approve the fueling plan as drafted.**\n\n1. (Lean) Approve as-is, one week of trial\n2. Adjust the calorie targets first\n3. Hold for more data\n\n**Default if no choice by 2026-09-30:** option 1\n", encoding="utf-8")
    s, d = get("/api/decide")
    oh = next((x for x in d["decisions"] if x["path"].endswith("oh-decision.md")), None)
    okc = (oh is not None
           and oh.get("ask") and "fueling plan" in oh["ask"]
           and oh.get("why_now") and "six weeks out" in oh["why_now"]
           and oh.get("source") == "Marathon Roadmap"
           and len(oh.get("related", [])) >= 1
           and oh.get("can_view_full") is True
           and oh.get("options") and len(oh["options"]) == 3)
    print("\n[PASS] /api/decide context surface (ask+why_now+source+related+full)" if okc
          else "\n[FAIL] decide context surface: %r" % (oh or {}))
    # full-item read proxy: returns the body, no raw path token semantics leak
    s, fi = get("/api/decide-item?path=" + urllib.parse.quote(oh["path"]))
    okfull = fi.get("ok") is True and "fueling plan has to be settled" in fi.get("body", "") and fi.get("title")
    print("\n[PASS] /api/decide-item full-item open (body proxied, ok=%s)" % fi.get("ok") if okfull
          else "\n[FAIL] decide-item: %r" % fi)
    # OTHER free-type -> response: (Option A), YAML-safe multi-line, NEVER done-log,
    # NEVER pollutes counter. chosen absent so no done-log token.
    long_resp = "Approve it but re-check carbs after week one.\nAlso note: plan should be revisited at taper.\nQuote \"the gut\" handling."
    s, rr = post("/api/resolve", {"path": oh["path"], "response": long_resp})
    fm,_ = vaultlib.split_frontmatter((Path(tmp) / "01 Inbox" / "oh-decision.md").read_text())
    resp = fm.get("response", "")
    lx = Path(tmp) / "05 Assets" / "Data" / "done" / "completions.jsonl"
    logs2 = ""
    if lx.is_file():
        import json as _j2
        logs2 = "\n".join(_j2.loads(l)["task"] for l in lx.read_text().splitlines() if l.strip())
    okr2 = (rr.get("ok") is True
            and fm.get("decision") == "resolved"
            and "carbs after week one" in resp          # multi-line survives
            and "quot" in resp or "\"the gut\"" in resp # quotes survived
            and "fueling plan" not in logs2             # response NOT in done-log
            and "Approve it" not in logs2)
    print("\n[PASS] Other free-type persisted as response: (YAML-safe, not done-log)" if okr2
          else "\n[FAIL] other-response: resp=%r logged=%r rr=%r" % (resp, logs2, rr))
    # re-open: a NEW open decision whose option-tap (choose) -> chosen -> done-log
    (Path(tmp) / "01 Inbox" / "oh2.md").write_text(
        "---\ntype: inbox\ntitle: pick the venue\nowner: athena\nawaiting: joe\ndecision: open\ndue: \"2026-10-01\"\n---\n\n# The fork (why it matters now)\nVenue holds 200 and the date is pinned.\n\n1. Blue Room\n2. (Lean) Garden Hall\n3. Rooftop\n\n**Default if no choice by 2026-10-01:** option 2\n", encoding="utf-8")
    s, d2 = get("/api/decide")
    oh2 = next((x for x in d2["decisions"] if x["path"].endswith("oh2.md")), None)
    s, rr2 = post("/api/resolve", {"path": oh2["path"], "chosen": "Garden Hall"})
    logs3 = ""
    lx3 = Path(tmp) / "05 Assets" / "Data" / "done" / "completions.jsonl"
    if lx3.is_file():
        import json as _j3
        logs3 = "\n".join(_j3.loads(l)["task"] for l in lx3.read_text().splitlines() if l.strip())
    okr3 = rr2.get("logged") == "done-logged" and "Garden Hall" in logs3 and rr2.get("chosen") == "Garden Hall"
    print("\n[PASS] option-tap confirm records chosen in done-log (counter feed)" if okr3
          else "\n[FAIL] option-tap: rr=%r logged=%r" % (rr2, logs3))
except Exception as e:
    print("\n[FAIL] decide-overhaul: %s: %s" % (type(e).__name__, e))


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

# 3f) OMEGA P1 / Ω v15 (2026-09-22 Joe ruling): INBOX capture — POST
#     /api/add-capture now creates a KANBAN TRIAGE card assigned to hermes on
#     the work board (Hermes the orchestrator decomposes/scopes/assigns/routes),
#     NOT a vault inbox note. Routing lane: awaiting:hermes (default) -> triage
#     card; awaiting:joe (approval lane, unchanged) -> real 01 Inbox note.
#     Original-Text intact (raw capture rides unedited as title + body + a
#     "source: omega-capture" tag). No belt-and-suspenders inbox note on the
#     hermes lane. KANBAN_DB is pinned to a throwaway file so the test never
#     touches the live fleet board.
try:
    orig = "Omega inbox capture test idea"
    inbox_dir = Path(tmp) / "01 Inbox"
    inbox_before = len(list(inbox_dir.rglob("*.md"))) if inbox_dir.exists() else 0
    s, d = post("/api/add-capture", {"text": orig})
    ok_card = d.get("ok") is True and bool(d.get("card_id")) and d.get("source") == "omega-capture"
    # no inbox note written on the hermes lane (no belt-and-suspenders): the
    # capture must NOT add a new 01 Inbox note (fixtures seed the dir upfront).
    inbox_after = len(list(inbox_dir.rglob("*.md"))) if inbox_dir.exists() else 0
    ok_no_inbox = (inbox_after - inbox_before) == 0
    # Original-Text intact: the STORED card body carries the raw text + tag.
    # Read it back from the throwaway engine DB (the isolated subprocess wrote
    # there via KANBAN_DB set in the boot env).
    ok_ot = False
    body_txt = ""
    try:
        import sqlite3 as _sq
        _con = _sq.connect(kanban_tmp)
        row = _con.execute("SELECT title, body FROM tasks WHERE id=?",
                           (d.get("card_id"),)).fetchone()
        _con.close()
        if row:
            title_t, body_txt = (row[0] or ""), (row[1] or "")
            ok_ot = (body_txt.startswith(orig) and "source: omega-capture" in body_txt
                     and (title_t == orig[:120]))
    except Exception:
        ok_ot = False
    prints = (ok_card, ok_no_inbox, ok_ot, not any(t in str({k: v for k, v in d.items() if k != "path"})
                   for t in ["sched.py", "WRITABLE_KEYS", "status:", "awaiting:"]))
    # JOE override via the selector: awaiting:joe -> approval lane UNCHANGED
    # (a real 01 Inbox note awaiting joe), surfaced on the Decide lane.
    s3, d3 = post("/api/add-capture", {"text": "Omega joe-lane capture test", "awaiting": "joe"})
    ok_override = d3.get("ok") is True and d3.get("path", "").startswith("01 Inbox/")
    pj = Path(tmp) / d3["path"]
    fmj, _ = (vaultlib.split_frontmatter(pj.read_text(encoding="utf-8"))
              if pj.exists() else ({}, ""))
    joe_lane = fmj.get("awaiting") == "joe" and fmj.get("decision") == "open"
    s4, d4 = get("/api/decide")
    joe_in_decide = any(x.get("path") == d3["path"] for x in d4["decisions"])
    prints2 = (ok_override, joe_lane, joe_in_decide)

    ok = all(prints) and all(prints2)
    print("\n[PASS] Ωv15 inbox-capture -> kanban triage card (hermes) + OT intact + "
          "no inbox note + joe approval lane unchanged "
          "(card=%s, acpts=%s) + JOE override=%s" %
          (d.get("card_id"), prints, prints2) if ok
          else "\n[FAIL] inbox-capture v15: acpts=%s joe-override=%s d=%r d3=%r fmj=%r" %
          (prints, prints2, d, d3, fmj))
    try:
        if kanban_tmp and os.path.exists(kanban_tmp):
            os.remove(kanban_tmp)
    except OSError:
        pass
except Exception as e:
    print("\n[FAIL] inbox-capture round-trip: %s: %s" % (type(e).__name__, e))

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

# 3e) ROUND-12: RENAME round-trip (inline edit) — renew the quick-add note
#   and rename via /api/rename; body checkbox + H1 reflect; title unchanged prose.
try:
    s, d = post("/api/add", {"text": "round12 rename me task"})
    rp = d["path"]
    s, rr = post("/api/rename", {"path": rp, "title": "round12 renamed task"})
    txt = (Path(tmp) / rp).read_text(encoding="utf-8")
    fm_r, _ = vaultlib.split_frontmatter(txt)
    ok = rr.get("ok") and fm_r.get("title") == "round12 renamed task" \
        and "# round12 renamed task" in txt and "- [ ] round12 renamed task" in txt \
        and "round12 rename me task" not in txt
    print("\n[PASS] /api/rename round-trip (title+checkbox+H1, prose-safe)" if ok
          else "\n[FAIL] rename: rr=%r fm=%r" % (rr, fm_r))
except Exception as e:
    print("\n[FAIL] rename: %s: %s" % (type(e).__name__, e))

# 3f) ROUND-12: UNDO — done then undone restores status open + removes /done log
try:
    s, d = post("/api/add", {"text": "round12 undo me task"})
    up = d["path"]
    s, dn = post("/api/done", {"path": up})
    logp = Path(tmp) / "05 Assets" / "Data" / "done" / "completions.jsonl"
    had = logp.is_file() and "round12 undo me task" in logp.read_text()
    s, ud = post("/api/undone", {"path": up})
    fm_u, _ = vaultlib.split_frontmatter((Path(tmp) / up).read_text())
    removed = logp.is_file() and "round12 undo me task" not in logp.read_text()
    print("\n[PASS] /api/undone reopens+removes done-log (status=%r, log-gone=%s)" %
          (fm_u.get("status"), removed) if (ud.get("ok") and fm_u.get("status") == "open" and removed)
          else "\n[FAIL] undo: ud=%r status=%r had=%s" % (ud, fm_u.get("status"), had))
except Exception as e:
    print("\n[FAIL] undo: %s: %s" % (type(e).__name__, e))

# 3g) ROUND-12: PLATE restore — /api/open returns plate so a returning Pick
#   view can restore selections (frontend keeps a localStorage mirror too).
try:
    s, d = post("/api/plate", {"paths": ["02 Planner/Tasks/open/deselect-task.md"]})
    s, o = get("/api/open")
    ok = o.get("plate") == ["02 Planner/Tasks/open/deselect-task.md"] \
        and any(x["path"] == "02 Planner/Tasks/open/deselect-task.md" and x["on_plate"] for x in o["items"])
    print("\n[PASS] /api/open returns plate (restoreable on view-return)" if ok
          else "\n[FAIL] plate-restore: plate=%r items-has-on_plate=%r" % (o.get("plate"), [x["path"] for x in o["items"] if x.get("on_plate")]))
    s, d = post("/api/plate", {"paths": []})  # reset
except Exception as e:
    print("\n[FAIL] plate-restore: %s: %s" % (type(e).__name__, e))

# 3h) ROUND-12 CRITICAL: set_frontmatter must NOT clobber existing list tags
#   (previously flattened tags: [a,b] -> tags:"" on every write-back). A
#   write-back of an unrelated field must preserve tags + quoted dates + prose.
try:
    note2 = "02 Planner/Tasks/open/tags-test.md"
    (Path(tmp) / "02 Planner" / "Tasks" / "open").mkdir(parents=True, exist_ok=True)
    (Path(tmp) / note2).write_text(
        "---\ntitle: \"tags demo\"\ntype: task\nstatus: open\nowner: hermes\ncreated: \"2026-09-14\"\ntags:\n  - home\n  - emily\n  - task\n---\n\n# tags demo\n\noriginal prose untouched\n",
        encoding="utf-8")
    # point the IN-PROCESS vaultlib at the temp vault (the HTTP subprocess
    # already has VAULT_ROOT=tmp; this process's vaultlib must not touch real)
    vaultlib.VAULT_ROOT = Path(tmp)
    vaultlib.set_frontmatter(note2, {"status": "done"})
    txt2 = (Path(tmp) / note2).read_text(encoding="utf-8")
    fm2, body2 = vaultlib.split_frontmatter(txt2)
    ok = fm2.get("tags") == ["home", "emily", "task"] \
        and fm2.get("status") == "done" and fm2.get("created") == "2026-09-14" \
        and "original prose untouched" in body2 \
        and 'tags: ""' not in txt2 and (Path(tmp)/note2).name in note2
    print("\n[PASS] set_frontmatter preserves list tags + quoted dates (tags=%r)" % fm2.get("tags") if ok
          else "\n[FAIL] tags-clobber: tags=%r txt=%r" % (fm2.get("tags"), txt2[:200]))
except Exception as e:
    print("\n[FAIL] tags-clobber: %s: %s" % (type(e).__name__, e))

# 4) resolve only updates frontmatter — body untouched (Original Text safe)
try:
    fm, body = vaultlib.split_frontmatter((root / "01 Inbox/decide-something.md").read_text())
    ok = "# body" in body and "awaiting" in fm
    print("\n[PASS] resolve left body intact (Original-Text safe)" if ok else "\n[FAIL] body mutated")
except Exception as e:
    print("\n[FAIL] body-check: %s: %s" % (type(e).__name__, e))

# 4b) Ωv17 "IN THE WORKS" STRIP flow — GET /api/triage-flow now mirrors EVERY
#   active card on the work board (ANY assignee) in an active section, labels
#   each with its REAL kanban section (triage/todo/ready/running/review/blocked),
#   flags needs-you (review / blocked-needs_input) separately, keeps done →
#   recently filed, dims bot-automation backfill (kept visible), and NEVER leaks
#   a card id / path / seam. Pure read (auth-free). Re-seed the server's
#   kanban_tmp with our throwaway cards.
#   NOTE: the test table now carries a `body` column (get_triage_flow SELECTs
#   it for the automation scan) plus an apollo + a nightly-sweep card to prove
#   the mirror includes non-hermes lanes and the noise guard.
try:
    import sqlite3 as _sq3
    _con = _sq3.connect(kanban_tmp)
    _con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY,title TEXT,body TEXT,assignee TEXT,status TEXT,tenant TEXT,created_at INT,block_kind TEXT)")
    _seed = [
        ("t10", "fresh capture idea",  None, "hermes",    "triage",   "work", 1, None),
        ("t11", "building it",         None, "hermes",    "running",  "work", 2, None),
        ("t12", "t_12 .md source: x",  None, "hermes",    "running",  "work", 3, None),
        ("t13", "marathon fueling",    None, "hermes",    "review",   "work", 4, None),
        ("t14", "blocked on you",      None, "hermes",    "blocked",  "work", 5, "needs_input"),
        ("t15", "dependency noise",    None, "hermes",    "blocked",  "work", 6, "dependency"),
        ("t16", "vesta filing",        None, "vesta",     "running",  "work", 7, None),
        ("t17", "filed to library",    None, "hermes",    "done",     "work", 8, None),
        ("t18", "apollo chore",        None, "apollo",    "running",  "work", 9, None),
        ("t19", "nightly zone sweep",  None, "hephaestus","running",  "work", 10, None),
    ]
    _con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", _seed)
    _con.commit(); _con.close()
    _s, _d = get("/api/triage-flow")
    ok_t = _d.get("ok") is True
    _in = _d.get("in_flight") or []
    _needs = _d.get("needs_you") or []
    _f = _d.get("recent_filed") or []
    def _has(_arr, _s_):
        return any(_s_ in x["title"] for x in _arr)
    # needs-you = review + blocked-needs_input, NOT dependency/transient
    ok_needs = (_has(_needs, "marathon fueling") and _has(_needs, "blocked on you")
                and not _has(_needs, "dependency noise"))
    # live mirror = ANY assignee in an active section (apollo + vesta + heph)
    ok_mirror = (_has(_in, "apollo chore") and _has(_in, "vesta filing")
                 and _has(_in, "building it") and _has(_in, "fresh capture idea"))
    # real section tag surfaced per item (board section verbatim, not a life-word)
    _secs = {x["title"]: x.get("section") for x in _in}
    ok_sections = (_secs.get("building it") == "running" and
                   _secs.get("fresh capture idea") == "triage" and
                   _secs.get("marathon fueling") == "review" and
                   _secs.get("nightly zone sweep") == "running")
    # automation backfill is kept visible but dim (auto flag), never hidden
    _auto_titles = [x["title"] for x in _in if x.get("auto")]
    ok_auto = ("nightly zone sweep" in _auto_titles) and \
              all(t not in _auto_titles for t in ("building it", "fresh capture idea"))
    ok_filed = _has(_f, "filed to library")
    ok_count = _d.get("count") == 9          # 9 active cards (done t17 excluded)
    # no-crud audit across every rendered value (titles scrubbed)
    _blob = " ".join(str(x) for _b_ in ("in_flight", "needs_you", "recent_filed")
                     for x in _d.get(_b_, []))
    _blob += " " + str(_d.get("empty_last_filed"))
    ok_nocrud = all(tok not in _blob for tok in
                    ("t_", "/opt/", ".md", "status:", "awaiting:", "source:", "tenant:", "sched.py"))
    ok = ok_t and ok_needs and ok_mirror and ok_sections and ok_auto and ok_filed and ok_count and ok_nocrud and (_s == 200)
    print("\n[PASS] Ωv17 triage-flow board-mirror + sections + no-crud (count=%s, in=%d, needs=%d, filed=%d)"
          % (_d.get("count"), len(_in), len(_needs), len(_f))
          if ok else
          "\n[FAIL] triage-flow: ok=%s needs=%s mirror=%s sections=%s auto=%s filed=%s count=%s nocrud=%s d=%r"
          % (ok_t, ok_needs, ok_mirror, ok_sections, ok_auto, ok_filed, ok_count, ok_nocrud, _d))
    try:
        if kanban_tmp and os.path.exists(kanban_tmp):
            os.remove(kanban_tmp)
    except OSError:
        pass
except Exception as e:
    print("\n[FAIL] triage-flow: %s: %s" % (type(e).__name__, e))

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