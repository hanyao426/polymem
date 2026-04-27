"""$PMEM context block generator.

Adapted from claude-mem's context-generator.cjs.
Produces a compressed context block injected on session start.

Differences from claude-mem:
  - `$PMEM` header instead of `$CMEM`
  - Per-client filtering (show observations from any client by default, or filter)
  - Cross-client unified timeline
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from .db import Database


TYPE_EMOJI = {
    "bugfix": "🔴",
    "feature": "🟣",
    "refactor": "🔄",
    "change": "✅",
    "discovery": "🔵",
    "decision": "⚖️",
}

# Visually distinct two-letter client badges. Uses non-overlapping pairs so
# clients don't shadow each other in the $PMEM block (e.g. "cl" was ambiguous
# between claude_code and cline). Unknown clients fall back to first 2 chars.
CLIENT_BADGE = {
    "claude_code": "cc",
    "codex":       "cx",
    "cursor":      "cu",
    "cline":       "cn",
    "windsurf":    "ws",
    "gemini_cli":  "gm",
    "chatgpt":     "gp",
    "aider":       "ai",
    "manual":      "mn",
}


def _client_badge(client: str) -> str:
    return CLIENT_BADGE.get(client, (client or "??")[:2])


@dataclass
class ContextOptions:
    project: str
    client: Optional[str] = None  # filter by client, None = all clients
    max_observations: int = 50
    max_summaries: int = 5
    full_narrative_count: int = 0  # number of recent obs to expand fully
    show_last_summary: bool = True
    days: Optional[int] = None  # only include observations from last N days (None = no time filter)
    lite: bool = False  # lite mode: titles only, no summary expansion, MCP-pointer hint


def generate_context(db: Database, opts: ContextOptions) -> str:
    """Generate the $PMEM context block for injection at session start."""
    if opts.lite:
        # Lite mode forces compact output regardless of caller's other settings
        opts.full_narrative_count = 0
        opts.show_last_summary = False

    now_iso, _ = db.now()
    header_hint = (
        "Fetch details via MCP: memory_get / memory_search / memory_recall_full"
        if opts.lite
        else "Fetch details: memory_get_observations([IDs])"
    )
    lines = [
        f"# $PMEM {opts.project} {now_iso}" + ("  (lite index)" if opts.lite else ""),
        "",
        "Legend: 🔴bugfix 🟣feature 🔄refactor ✅change 🔵discovery ⚖️decision",
        "Clients: [cc]=claude_code [cx]=codex [cu]=cursor [cn]=cline [ws]=windsurf [gm]=gemini_cli",
        "Format: ID TIME CLIENT TYPE TITLE",
        header_hint,
        "",
    ]

    filters = ["project = ?"]
    params: list = [opts.project]
    if opts.client:
        filters.append("client = ?")
        params.append(opts.client)
    if opts.days is not None and opts.days > 0:
        cutoff = int(time.time() * 1000) - opts.days * 86_400_000
        filters.append("created_at_epoch >= ?")
        params.append(cutoff)

    query = f"""
        SELECT id, client, type, title, subtitle, narrative, facts,
               created_at_epoch, discovery_tokens
        FROM observations
        WHERE {' AND '.join(filters)}
        ORDER BY created_at_epoch DESC
        LIMIT ?
    """
    rows = db.conn.execute(query, (*params, opts.max_observations)).fetchall()

    # Stats
    total_read_tokens = 0
    total_work_tokens = 0
    for r in rows:
        read_tokens = (len(r["title"] or "") + len(r["subtitle"] or "")) // 4
        total_read_tokens += read_tokens
        total_work_tokens += r["discovery_tokens"] or 0

    savings = 0
    if total_work_tokens > 0:
        savings = int((1 - total_read_tokens / total_work_tokens) * 100)

    lines.append(
        f"Stats: {len(rows)} obs ({total_read_tokens:,}t read) | "
        f"{total_work_tokens:,}t work | {savings}% savings"
    )
    lines.append("")

    # Group by day
    day_groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        day = time.strftime("%b %d, %Y", time.gmtime(r["created_at_epoch"] / 1000))
        day_groups.setdefault(day, []).append(r)

    for day, items in day_groups.items():
        lines.append(f"### {day}")
        for i, r in enumerate(items):
            hm = time.strftime("%I:%M%p", time.gmtime(r["created_at_epoch"] / 1000)).lower().lstrip("0")
            emoji = TYPE_EMOJI.get(r["type"], "·")
            # Show client badge
            client_badge = f"[{_client_badge(r['client'])}]"
            line = f"{r['id']} {hm} {client_badge} {emoji} {r['title'] or '(untitled)'}"
            lines.append(line)

            # Expand narrative for top N recent obs
            if i < opts.full_narrative_count:
                if r["subtitle"]:
                    lines.append(f"    ↳ {r['subtitle']}")
        lines.append("")

    # Last summary
    if opts.show_last_summary:
        sum_filters = ["project = ?"]
        sum_params: list = [opts.project]
        if opts.client:
            sum_filters.append("client = ?")
            sum_params.append(opts.client)
        summary_row = db.conn.execute(
            f"""
            SELECT client, request, investigated, learned, completed, next_steps
            FROM session_summaries
            WHERE {' AND '.join(sum_filters)}
            ORDER BY created_at_epoch DESC
            LIMIT 1
            """,
            sum_params,
        ).fetchone()
        if summary_row:
            lines.append(f"### Session Summary (last, from {summary_row['client']})")
            for field in ("request", "investigated", "learned", "completed", "next_steps"):
                val = summary_row[field]
                if val:
                    lines.append(f"**{field.replace('_', ' ').title()}**: {val}")
            lines.append("")

    return "\n".join(lines)
