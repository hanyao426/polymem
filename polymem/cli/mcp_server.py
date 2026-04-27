"""PolyMem MCP stdio server — unified read interface for all clients.

Any MCP-capable client (Claude Code, Cursor, Codex, Cline, ChatGPT Desktop,
Gemini CLI, ...) can connect and query memory via 5 tools.
"""

from __future__ import annotations

import json
import sys
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    sys.stderr.write(
        "[polymem:mcp] missing dependency `mcp`. "
        "Run: pipx install --force --with mcp polymem\n"
    )
    raise

from .client import PolyMemClient


server: Server = Server("polymem")
api = PolyMemClient()


# ─── Tool registry ─────────────────────────────────────────────────────────


TOOLS: list[Tool] = [
    Tool(
        name="memory_search",
        description=(
            "Search cross-client memory (observations from Claude Code, Codex, "
            "Cursor, Cline, etc.). FTS5 + vector hybrid. Returns IDs + titles."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {"type": "string"},
                "client": {
                    "type": "string",
                    "description": (
                        "Filter by client: claude_code / codex / cursor / cline / ..."
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": [
                        "bugfix", "feature", "refactor", "change", "discovery", "decision",
                    ],
                },
                "limit": {"type": "number", "default": 20},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="memory_get",
        description="Fetch full observation details by ID (batch).",
        inputSchema={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "number"}},
            },
            "required": ["ids"],
        },
    ),
    Tool(
        name="memory_context",
        description=(
            "Get the $PMEM context block. Shows recent observations across all clients."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "client": {"type": "string"},
                "max_obs": {"type": "number", "default": 50},
                "lite": {"type": "boolean", "default": False},
                "days": {"type": "number"},
            },
            "required": ["project"],
        },
    ),
    Tool(
        name="memory_kg_query",
        description="Query the knowledge graph for entity relations.",
        inputSchema={
            "type": "object",
            "properties": {
                "entity": {"type": "string"},
                "as_of": {"type": "string", "description": "ISO date for temporal filter"},
                "direction": {
                    "type": "string",
                    "enum": ["outgoing", "incoming", "both"],
                },
            },
            "required": ["entity"],
        },
    ),
    Tool(
        name="memory_recall_full",
        description=(
            "Fetch full-text conversation backup (raw messages, not extracted observations)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_session_id": {"type": "string"},
                "limit": {"type": "number", "default": 100},
            },
            "required": ["memory_session_id"],
        },
    ),
]


# ─── Handlers ──────────────────────────────────────────────────────────────


@server.list_tools()
async def _list_tools() -> list[Tool]:
    return TOOLS


def _ok(data: Any) -> list[TextContent]:
    text = data if isinstance(data, str) else json.dumps(data, indent=2, ensure_ascii=False)
    return [TextContent(type="text", text=text)]


@server.call_tool()
async def _call_tool(name: str, args: dict[str, Any]) -> list[TextContent]:
    args = args or {}
    try:
        if name == "memory_search":
            kwargs = {"query": args["query"]}
            for k in ("project", "client", "type"):
                if args.get(k) is not None:
                    kwargs[k] = args[k]
            if args.get("limit") is not None:
                kwargs["limit"] = args["limit"]
            return _ok(api.search(**kwargs))

        if name == "memory_get":
            ids = args.get("ids") or []
            return _ok(api.get_observations([int(i) for i in ids]))

        if name == "memory_context":
            ctx = api.get_context(
                project=args["project"],
                client=args.get("client"),
                max_obs=args.get("max_obs"),
                lite=bool(args.get("lite")),
                days=args.get("days"),
            )
            return _ok(ctx)

        if name == "memory_kg_query":
            return _ok(api.kg_query(
                entity=args["entity"],
                as_of=args.get("as_of"),
                direction=args.get("direction") or "both",
            ))

        if name == "memory_recall_full":
            return _ok(api.raw_session(
                memory_session_id=args["memory_session_id"],
                limit=int(args.get("limit") or 100),
            ))

        raise ValueError(f"unknown tool: {name}")
    except Exception as e:
        # MCP SDK turns exceptions into JSON-RPC errors automatically; we just
        # surface them with context.
        sys.stderr.write(f"[polymem:mcp] {name} failed: {type(e).__name__}: {e}\n")
        raise


# ─── Boot ──────────────────────────────────────────────────────────────────


async def _async_main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    import asyncio
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        sys.stderr.write(f"[polymem:mcp] fatal: {type(e).__name__}: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
