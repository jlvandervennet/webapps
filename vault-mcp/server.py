#!/usr/bin/env python3
"""
vault MCP server — Hephaestus build for Joe's stack, 2026-09-14.

Exposes the vault (the single source of truth at /opt/data/Second-Brain) as MCP
tools: read notes, search, and frontmatter-safe writes that enforce GL-002
conventions and refuse to touch the user's Original Text (CLAUDE.md hard rule #1).

The tool logic delegates to vaultlib.py — the SAME core the Joe's Cockpit PWA
backend will import, so agents and the app are consistent by construction.

Pure stdlib + the mcp SDK (same pattern as /opt/data/icloud-mcp/server.py).
"""

import json
import sys

import vaultlib

VERSION = "0.1.0"


# ── Tool handlers (thin wrappers over vaultlib) ────────────────────────────
def _read(args):
    return vaultlib.read_note(args["path"])


def _search(args):
    return {"results": vaultlib.search_notes(args["query"], args.get("include_path"), args.get("limit", 50))}


def _write(args):
    return vaultlib.set_frontmatter(args["path"], args["updates"])


def _decisions(args):
    return {"decisions": vaultlib.get_joe_decisions()}


def _daily(args):
    return vaultlib.get_daily_3(args.get("day", ""))


TOOLS = [
    {
        "name": "vault_read",
        "description": (
            "Read a vault note by vault-relative path (e.g. "
            "'01 Inbox/Decision - X (awaiting Joe).md'). Returns parsed "
            "frontmatter, body text, and mtime. Read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "vault-relative note path"}},
            "required": ["path"],
        },
        "handler": _read,
    },
    {
        "name": "vault_search",
        "description": "Case-insensitive text search across vault markdown. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "include_path": {"type": "string", "description": "optional path substring filter"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        "handler": _search,
    },
    {
        "name": "vault_frontmatter_write",
        "description": (
            "Frontmatter-safe write: update ONLY approved fields on an "
            "existing note (status, assignee, due, related, awaiting, decision, "
            "do-by, default, progress, next_step, processed*). Enforces GL-002 "
            "(refuses unapproved keys) and NEVER edits the note body / the "
            "user's Original Text (CLAUDE.md hard rule #1). Requires the note "
            "to have a frontmatter block."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "vault-relative note path"},
                "updates": {
                    "type": "object",
                    "description": "approved key->value frontmatter updates",
                },
            },
            "required": ["path", "updates"],
        },
        "handler": _write,
    },
    {
        "name": "vault_joe_decisions",
        "description": "All inbox notes awaiting Joe, decision open/due — the cockpit's decide-by surface. Read-only.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": _decisions,
    },
    {
        "name": "vault_today",
        "description": "Parse today's daily scratchpad: the 'pick your 3' unchecked items + completion counter. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {"day": {"type": "string", "description": "YYYY-MM-DD; defaults to today"}},
        },
        "handler": _daily,
    },
]


# ── MCP server (low-level stdio) ───────────────────────────────────────────
def build_server():
    from mcp.server.lowlevel import Server
    import mcp.types as types

    async def handle_list_tools(ctx, params):
        tools = [
            types.Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"])
            for t in TOOLS
        ]
        return types.ListToolsResult(tools=tools)

    async def handle_call_tool(ctx, params):
        name = params.name
        args = params.arguments or {}
        for t in TOOLS:
            if t["name"] == name:
                try:
                    result = t["handler"](args)
                    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
                    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])
                except Exception as e:
                    return types.CallToolResult(
                        is_error=True,
                        content=[types.TextContent(type="text", text="%s: %s" % (type(e).__name__, e))],
                    )
        return types.CallToolResult(
            is_error=True,
            content=[types.TextContent(type="text", text="unknown tool: %s" % name)],
        )

    server = Server(
        "vault",
        version=VERSION,
        title="Vault access (read/search/frontmatter-safe-write)",
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )
    return server


def main():
    import anyio
    from mcp.server import stdio

    server = build_server()

    async def run():
        async with stdio.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    main()