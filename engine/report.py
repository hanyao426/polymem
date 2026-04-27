"""Daily report generator for PolyMem.

Turns today's observations + summaries into a Markdown work journal.
No additional LLM calls — the data is already structured by the extraction
pipeline, so this module just aggregates + templates.

Theme clustering strategy (simple and cheap):
  - Group observations within each type by shared files_modified paths
  - Fall back to concept-tag grouping when no file overlap exists
  - Single-topic observations stay ungrouped
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .db import Database


TYPE_EMOJI = {
    "bugfix": "🔴",
    "feature": "🟣",
    "refactor": "🔄",
    "change": "✅",
    "discovery": "🔵",
    "decision": "⚖️",
}

TYPE_LABEL = {
    "bugfix": "Bug 修复",
    "feature": "功能实现",
    "refactor": "重构",
    "change": "变更",
    "discovery": "发现",
    "decision": "决策",
}


@dataclass
class ReportOptions:
    date: Optional[str] = None  # YYYY-MM-DD, default: today
    project: Optional[str] = None
    client: Optional[str] = None
    include_next_steps: bool = True
    group_by_theme: bool = True


# ─── Theme extraction ──────────────────────────────────────────────────────


def _extract_theme_key(obs: dict[str, Any]) -> str:
    """Derive a theme key from files_modified, files_read, or concept tags."""
    files_mod = obs.get("files_modified") or []
    if isinstance(files_mod, str):
        try:
            files_mod = json.loads(files_mod)
        except json.JSONDecodeError:
            files_mod = []

    if files_mod:
        # Use the top-level component from the first modified file
        path = files_mod[0]
        parts = path.split("/")
        # Find something more meaningful than generic "src" / "lib"
        for p in parts:
            if p and p not in ("src", "lib", "app", "pages", "components", "services"):
                return p
        return parts[-1] if parts else "misc"

    # Fall back to longest file_read path component
    files_read = obs.get("files_read") or []
    if isinstance(files_read, str):
        try:
            files_read = json.loads(files_read)
        except json.JSONDecodeError:
            files_read = []
    if files_read:
        parts = files_read[0].split("/")
        for p in parts:
            if p and p not in ("src", "lib", "app", "pages", "components", "services"):
                return p

    # Last resort: first concept tag
    concepts = obs.get("concepts") or []
    if isinstance(concepts, str):
        try:
            concepts = json.loads(concepts)
        except json.JSONDecodeError:
            concepts = []
    if concepts:
        return concepts[0]

    return "misc"


def _group_by_theme(observations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for o in observations:
        key = _extract_theme_key(o)
        groups.setdefault(key, []).append(o)
    return groups


# ─── Data fetch ────────────────────────────────────────────────────────────


def _fetch_observations(db: Database, opts: ReportOptions) -> list[dict[str, Any]]:
    date_prefix = opts.date or datetime.now().strftime("%Y-%m-%d")
    filters = ["created_at LIKE ?"]
    params: list[Any] = [f"{date_prefix}%"]
    if opts.project:
        filters.append("project = ?")
        params.append(opts.project)
    if opts.client:
        filters.append("client = ?")
        params.append(opts.client)

    rows = db.conn.execute(
        f"""
        SELECT id, client, project, type, title, subtitle, narrative,
               facts, concepts, files_read, files_modified,
               discovery_tokens, created_at_epoch
        FROM observations
        WHERE {' AND '.join(filters)}
        ORDER BY created_at_epoch ASC
        """,
        params,
    ).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        for k in ("facts", "concepts", "files_read", "files_modified"):
            try:
                d[k] = json.loads(d[k] or "[]")
            except json.JSONDecodeError:
                d[k] = []
        result.append(d)
    return result


def _fetch_recent_next_steps(db: Database, opts: ReportOptions, limit: int = 3) -> list[str]:
    """Get the N most recent non-empty next_steps from today's summaries."""
    date_prefix = opts.date or datetime.now().strftime("%Y-%m-%d")
    filters = ["created_at LIKE ?", "next_steps IS NOT NULL", "trim(next_steps) != ''"]
    params: list[Any] = [f"{date_prefix}%"]
    if opts.project:
        filters.append("project = ?")
        params.append(opts.project)
    if opts.client:
        filters.append("client = ?")
        params.append(opts.client)

    rows = db.conn.execute(
        f"""
        SELECT next_steps
        FROM session_summaries
        WHERE {' AND '.join(filters)}
        ORDER BY created_at_epoch DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [r["next_steps"] for r in rows if r["next_steps"]]


# ─── Markdown rendering ────────────────────────────────────────────────────


def _render_markdown(
    obs: list[dict[str, Any]],
    next_steps: list[str],
    opts: ReportOptions,
) -> str:
    date_str = opts.date or datetime.now().strftime("%Y-%m-%d")
    lines: list[str] = [f"# {date_str} 工作日报"]
    if opts.project or opts.client:
        scope = []
        if opts.project:
            scope.append(f"项目：{opts.project}")
        if opts.client:
            scope.append(f"客户端：{opts.client}")
        lines.append(f"> {' · '.join(scope)}")
    lines.append("")

    if not obs:
        lines.append("*今日暂无结构化观察*")
        return "\n".join(lines)

    # Overview
    type_counts: dict[str, int] = {}
    total_tokens = 0
    for o in obs:
        type_counts[o["type"]] = type_counts.get(o["type"], 0) + 1
        total_tokens += o.get("discovery_tokens") or 0

    overview_parts = []
    for t in ("feature", "bugfix", "refactor", "change", "discovery", "decision"):
        n = type_counts.get(t, 0)
        if n > 0:
            overview_parts.append(f"{TYPE_EMOJI[t]} {n} {TYPE_LABEL[t]}")
    lines.append("## 今日概览")
    lines.append("")
    lines.append(" · ".join(overview_parts))
    lines.append("")
    lines.append(f"> {len(obs)} 条观察 · {total_tokens:,} tokens consumed")
    lines.append("")

    # Per-type sections
    type_order = ("feature", "bugfix", "refactor", "decision", "change", "discovery")
    for t in type_order:
        items = [o for o in obs if o["type"] == t]
        if not items:
            continue
        lines.append(f"## {TYPE_EMOJI[t]} {TYPE_LABEL[t]} ({len(items)})")
        lines.append("")

        if opts.group_by_theme and len(items) >= 3:
            groups = _group_by_theme(items)
            # Sort groups by size descending
            sorted_groups = sorted(groups.items(), key=lambda kv: -len(kv[1]))
            for theme, gitems in sorted_groups:
                if len(gitems) >= 2:
                    lines.append(f"### {theme}")
                    for o in gitems:
                        lines.append(f"- **#{o['id']}** {o['title']}")
                        if o.get("subtitle"):
                            lines.append(f"  - {o['subtitle']}")
                    lines.append("")
                else:
                    # Singleton — list flat
                    for o in gitems:
                        lines.append(f"- **#{o['id']}** {o['title']}")
                        if o.get("subtitle"):
                            lines.append(f"  - {o['subtitle']}")
            lines.append("")
        else:
            for o in items:
                lines.append(f"- **#{o['id']}** {o['title']}")
                if o.get("subtitle"):
                    lines.append(f"  - {o['subtitle']}")
            lines.append("")

    # Next steps
    if opts.include_next_steps and next_steps:
        lines.append("## 📅 明日计划")
        lines.append("")
        lines.append("*（从最近的 session summaries 提取）*")
        lines.append("")
        for i, ns in enumerate(next_steps, 1):
            lines.append(f"**来源 {i}：**")
            # Split multi-line next_steps into bullets
            for para in ns.strip().split("\n"):
                para = para.strip()
                if not para:
                    continue
                if para.startswith(("-", "*", "•")):
                    lines.append(para)
                else:
                    lines.append(f"- {para}")
            lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"*Generated by PolyMem · {datetime.now().strftime('%Y-%m-%d %H:%M')}*")

    return "\n".join(lines)


# ─── Public API ────────────────────────────────────────────────────────────


def generate_report(db: Database, opts: ReportOptions) -> str:
    obs = _fetch_observations(db, opts)
    next_steps = _fetch_recent_next_steps(db, opts) if opts.include_next_steps else []
    return _render_markdown(obs, next_steps, opts)


def generate_report_json(db: Database, opts: ReportOptions) -> dict[str, Any]:
    """Structured JSON version for programmatic consumers."""
    obs = _fetch_observations(db, opts)
    next_steps = _fetch_recent_next_steps(db, opts) if opts.include_next_steps else []

    type_counts: dict[str, int] = {}
    for o in obs:
        type_counts[o["type"]] = type_counts.get(o["type"], 0) + 1

    groups_by_type: dict[str, dict[str, list[dict]]] = {}
    for t in ("feature", "bugfix", "refactor", "decision", "change", "discovery"):
        items = [o for o in obs if o["type"] == t]
        if not items:
            continue
        groups_by_type[t] = _group_by_theme(items) if opts.group_by_theme else {"all": items}

    return {
        "date": opts.date or datetime.now().strftime("%Y-%m-%d"),
        "project": opts.project,
        "client": opts.client,
        "total_observations": len(obs),
        "total_tokens": sum(o.get("discovery_tokens") or 0 for o in obs),
        "type_counts": type_counts,
        "groups_by_type": groups_by_type,
        "next_steps": next_steps,
    }
