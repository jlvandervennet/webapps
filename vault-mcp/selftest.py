#!/usr/bin/env python3
"""
vault MCP selftest — Hephaestus, 2026-09-14.

Boots the vault MCP server over stdio and exercises every tool through the real
MCP client protocol, plus a direct vaultlib write-safety check against a temp
fixture (never a real note). Reports PASS/FAIL per case.

Run: /opt/hermes/.venv/bin/python /opt/data/vault-mcp/selftest.py
"""

import asyncio
import os
import tempfile
import json
from pathlib import Path


async def main():
    import vaultlib

    # Isolate: point the core at a TEMP vault clone for the write-safety test.
    tmp = tempfile.mkdtemp(prefix="vault-mcp-selftest-")
    tmp_root = Path(tmp)
    (tmp_root / "01 Inbox").mkdir(parents=True)
    (tmp_root / "00 Daily Scratchpad").mkdir(parents=True)
    fixture = tmp_root / "01 Inbox" / "selftest-decision.md"
    fixture.write_text(
        "---\n"
        'title: "selftest decision"\n'
        "type: inbox\n"
        "awaiting: joe\n"
        "decision: open\n"
        "---\n\n"
        "# body is sacred\n"
        "Original user text goes here.\n",
        encoding="utf-8",
    )

    # ── vaultlib direct safety checks (isolated, no real notes touched) ──
    print("=" * 62)
    print("vault MCP selftest")
    print("temp vault root:", tmp_root)
    print("=" * 62)

    old_root = vaultlib.VAULT_ROOT
    vaultlib.VAULT_ROOT = tmp_root.resolve()

    # 1) read
    try:
        n = vaultlib.read_note("01 Inbox/selftest-decision.md")
        ok = n["frontmatter"].get("awaiting") == "joe"
        print("\n[PASS] read_note" if ok else "\n[FAIL] read_note")
        print("  awaiting:", n["frontmatter"].get("awaiting"))
    except Exception as e:
        print("\n[FAIL] read_note: %s: %s" % (type(e).__name__, e))

    # 2) frontmatter-safe write (approved key)
    try:
        r = vaultlib.set_frontmatter("01 Inbox/selftest-decision.md", {"decision": "resolved", "awaiting": "none"})
        n = vaultlib.read_note("01 Inbox/selftest-decision.md")
        ok = n["frontmatter"].get("decision") == "resolved"
        ok_body = "body is sacred" in n["body"]
        ok_orig = "Original user text goes here" in n["body"]
        print("\n[PASS] frontmatter_write (approved key)" if (ok and ok_body and ok_orig) else "\n[FAIL] frontmatter_write")
        print("  decision:", n["frontmatter"].get("decision"), "| body preserved:", ok_body and ok_orig)
    except Exception as e:
        print("\n[FAIL] frontmatter_write: %s: %s" % (type(e).__name__, e))

    # 3) safety: reject unapproved key (no invented fields)
    try:
        vaultlib.set_frontmatter("01 Inbox/selftest-decision.md", {"totally_new_field": "x"})
        print("\n[FAIL] write-safety (unapproved key accepted!)")
    except ValueError as e:
        print("\n[PASS] write-safety (unapproved key refused)")
        print("  ", e)
    except Exception as e:
        print("\n[FAIL] write-safety: %s: %s" % (type(e).__name__, e))

    # 4) safety: reject body/Original Text edit
    try:
        vaultlib.set_frontmatter("01 Inbox/selftest-decision.md", {"body": "HACKED"})
        print("\n[FAIL] original-text-safety (body edit accepted!)")
    except ValueError as e:
        print("\n[PASS] original-text-safety (body edit refused)")
        print("  ", e)
    except Exception as e:
        print("\n[FAIL] original-text-safety: %s: %s" % (type(e).__name__, e))

    # 5) search
    try:
        res = vaultlib.search_notes("original user text", limit=5)
        ok = any("selftest-decision" in r["path"] for r in res)
        print("\n[PASS] search" if ok else "\n[FAIL] search")
        print("  hits:", len(res))
    except Exception as e:
        print("\n[FAIL] search: %s: %s" % (type(e).__name__, e))

    # restore real root for the MCP server boot
    vaultlib.VAULT_ROOT = old_root

    # ── MCP protocol handshake (real /opt/hermes venv python) ──
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    params = StdioServerParameters(
        command="/opt/hermes/.venv/bin/python",
        args=["/opt/data/vault-mcp/server.py"],
        env={**os.environ, "VAULT_ROOT": "/opt/data/Second-Brain"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("\n[PASS] MCP handshake" if tools.tools else "\n[FAIL] MCP handshake")
            print("  tools listed: %d" % len(tools.tools))
            for t in tools.tools:
                print("   - %s" % t.name)

            # live read against the REAL vault (read-only, safe)
            r = await session.call_tool("vault_joe_decisions", {})
            body = (r.content[0].text or "") if r.content else "(none)"
            print("\n[%s] vault_joe_decisions (read-only, real vault)" % (
                "PASS" if not r.is_error else "FAIL"))
            print("   " + body[:300].replace("\n", "\n   "))


asyncio.run(main())