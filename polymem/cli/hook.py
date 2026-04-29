"""Claude Code hook dispatcher.

Invoked once per hook event by Claude Code. Reads stdin JSON, posts to engine,
writes JSON response to stdout (used by SessionStart for context injection).

Hot path — keep imports minimal. No fastapi / chromadb / pydantic imports here.
Cold start budget: ~80ms.
"""

from __future__ import annotations

import json
import os
import sys

from .client import PolyMemClient


SKIP_TOOLS = {
    "ListMcpResourcesTool",
    "SlashCommand",
    "Skill",
    "TodoWrite",
    "AskUserQuestion",
}
# Tool-name prefixes to skip — `mcp__polymem__*` are PolyMem reading itself,
# pure metadata noise that has no value as a "what the user did" observation.
SKIP_PREFIXES = ("mcp__polymem__",)


def _should_skip_tool(name: str) -> bool:
    return name in SKIP_TOOLS or any(name.startswith(p) for p in SKIP_PREFIXES)


def _read_stdin_json() -> dict:
    raw = sys.stdin.read() or ""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _project_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return os.environ.get("POLYMEM_PROJECT", "default")
    return os.path.basename(cwd.rstrip("/")) or "default"


def _emit_continue(extra: dict | None = None) -> None:
    out = {"continue": True, "suppressOutput": True}
    if extra:
        out.update(extra)
    sys.stdout.write(json.dumps(out))


def _emit_session_start(context: str, banner: str | None = None) -> None:
    out: dict = {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        },
    }
    if banner:
        out["systemMessage"] = banner
    sys.stdout.write(json.dumps(out))


def _resolve_session(api: PolyMemClient, hook_input: dict) -> str:
    sid = hook_input.get("session_id") or "unknown"
    project = _project_from_cwd(hook_input.get("cwd"))
    return api.session_init(
        client="claude_code",
        client_session_id=sid,
        project=project,
        model=hook_input.get("model"),
        user_prompt=hook_input.get("prompt"),
    )


def _cmd_session_init(api: PolyMemClient, hook_input: dict) -> None:
    _resolve_session(api, hook_input)
    _emit_continue()


def _cmd_observation(api: PolyMemClient, hook_input: dict) -> None:
    tool_name = hook_input.get("tool_name") or ""
    if _should_skip_tool(tool_name):
        _emit_continue()
        return
    mem_id = _resolve_session(api, hook_input)
    api.pending_observation(
        memory_session_id=mem_id,
        client="claude_code",
        tool_name=tool_name,
        tool_input=json.dumps(hook_input.get("tool_input") or {}, ensure_ascii=False),
        tool_response=json.dumps(hook_input.get("tool_response") or {}, ensure_ascii=False),
        cwd=hook_input.get("cwd"),
    )
    _emit_continue()


def _cmd_summarize(api: PolyMemClient, hook_input: dict) -> None:
    mem_id = _resolve_session(api, hook_input)
    api.pending_summary(
        memory_session_id=mem_id,
        client="claude_code",
        last_user_message=hook_input.get("last_user_message") or "",
        last_assistant_message=hook_input.get("last_assistant_message") or "",
    )
    _emit_continue()


def _cmd_session_complete(api: PolyMemClient, hook_input: dict) -> None:
    mem_id = _resolve_session(api, hook_input)
    api.session_complete(mem_id)
    _emit_continue()


def _cmd_context(api: PolyMemClient, hook_input: dict) -> None:
    """Full $PMEM block (heavy, ~3000 tokens)."""
    project = _project_from_cwd(hook_input.get("cwd"))
    ctx = api.get_context(project=project, client="claude_code")
    _emit_session_start(ctx)


def _cmd_context_lite(api: PolyMemClient, hook_input: dict) -> None:
    """Lightweight $PMEM index (~300 tokens), hybrid mode."""
    project = _project_from_cwd(hook_input.get("cwd"))
    days = int(os.environ.get("POLYMEM_LITE_DAYS", "3"))
    max_obs = int(os.environ.get("POLYMEM_LITE_MAX", "30"))
    ctx = api.get_context(
        project=project,
        client="claude_code",
        lite=True,
        days=days,
        max_obs=max_obs,
    )
    # Approximate obs count from formatted lines like "504 2:47am [cc] ..."
    import re
    obs_count = len(re.findall(r"^\d+\s+\d", ctx, re.MULTILINE))
    token_estimate = max(1, len(ctx) // 4)
    banner = (
        f"[PolyMem] injected $PMEM lite index — {obs_count} obs, "
        f"~{token_estimate} tokens, last {days}d (project={project}). "
        "Use MCP memory_search/memory_get for details."
    )
    _emit_session_start(ctx, banner=banner)


COMMANDS = {
    "session-init": _cmd_session_init,
    "observation": _cmd_observation,
    "summarize": _cmd_summarize,
    "session-complete": _cmd_session_complete,
    "context": _cmd_context,
    "context-lite": _cmd_context_lite,
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        sys.stderr.write(
            "usage: polymem hook <" + "|".join(COMMANDS.keys()) + ">\n"
        )
        return 1
    cmd = args[0]
    if cmd not in COMMANDS:
        sys.stderr.write(f"unknown hook event: {cmd}\n")
        return 1
    hook_input = _read_stdin_json()
    api = PolyMemClient()
    try:
        COMMANDS[cmd](api, hook_input)
    except Exception as e:
        # Hooks must NEVER block Claude Code. Swallow errors, log to stderr.
        sys.stderr.write(f"[polymem:hook:{cmd}] {type(e).__name__}: {e}\n")
        _emit_continue()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
