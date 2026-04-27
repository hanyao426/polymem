"""Codex CLI JSONL watcher.

Codex (unlike Claude Code) does not expose hooks. It writes every session as a
JSONL rollout file under ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl.

This watcher polls those files and incrementally streams new records to the
PolyMem engine via /v1/sessions/init + /v1/observations/pending +
/v1/summaries/pending. State is kept per-file (byte offset) so restarts are safe.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from .client import PolyMemClient


SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
STATE_PATH = Path.home() / ".polymem" / "codex-watcher-state.json"
DEFAULT_POLL_MS = int(os.environ.get("POLYMEM_CODEX_POLL_MS", "30000"))
DEFAULT_BACKFILL_DAYS = int(os.environ.get("POLYMEM_CODEX_BACKFILL_DAYS", "1"))
SKIP_TOOLS = {"update_plan"}


# ─── State persistence ──────────────────────────────────────────────────────


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"files": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ─── File discovery ─────────────────────────────────────────────────────────


def _find_session_files(cutoff_seconds: float) -> list[Path]:
    if not SESSIONS_ROOT.exists():
        return []
    now = time.time()
    out: list[Path] = []
    for year_dir in SESSIONS_ROOT.iterdir():
        if not year_dir.is_dir():
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir():
                continue
            for day_dir in month_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                for f in day_dir.iterdir():
                    if not (f.name.startswith("rollout-") and f.suffix == ".jsonl"):
                        continue
                    try:
                        if now - f.stat().st_mtime <= cutoff_seconds:
                            out.append(f)
                    except OSError:
                        pass
    return out


# ─── Per-file processing ────────────────────────────────────────────────────


def _process_file(path: Path, state: dict, api: PolyMemClient) -> None:
    raw = path.read_bytes()
    full_size = len(raw)
    entry = state["files"].get(str(path))
    if entry and entry["byteOffset"] >= full_size:
        return

    offset = entry["byteOffset"] if entry else 0
    slice_bytes = raw[offset:]
    last_nl = slice_bytes.rfind(b"\n")
    if last_nl < 0:
        return
    consumable = slice_bytes[: last_nl + 1]
    consumed_bytes = offset + len(consumable)

    cwd_hint: str | None = None

    for line in consumable.split(b"\n"):
        if not line.strip():
            continue
        try:
            d = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        payload = d.get("payload") or {}

        if d.get("type") == "session_meta" and entry is None:
            project = (payload.get("cwd") or "").rstrip("/").split("/")[-1] or "default"
            cwd_hint = payload.get("cwd")
            try:
                mem_id = api.session_init(
                    client="codex",
                    client_session_id=payload.get("id") or "unknown",
                    project=project,
                    model=payload.get("model_provider"),
                )
            except Exception as e:
                sys.stderr.write(f"[polymem:codex] session_init failed for {path.name}: {e}\n")
                return
            entry = state["files"][str(path)] = {
                "byteOffset": 0,
                "memorySessionId": mem_id,
                "pendingCalls": {},
                "lastUserMessage": "",
                "lastAgentMessage": "",
                "taskCompleteEmitted": False,
            }
            continue

        if entry is None:
            continue

        kind = (d.get("type"), payload.get("type"))

        if kind == ("response_item", "function_call"):
            entry["pendingCalls"][payload.get("call_id", "")] = {
                "name": payload.get("name") or "",
                "arguments": payload.get("arguments") or "",
            }
        elif kind == ("response_item", "function_call_output"):
            call = entry["pendingCalls"].pop(payload.get("call_id", ""), None)
            if call and call["name"] not in SKIP_TOOLS:
                output = payload.get("output")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                try:
                    api.pending_observation(
                        memory_session_id=entry["memorySessionId"],
                        client="codex",
                        tool_name=call["name"],
                        tool_input=call["arguments"],
                        tool_response=output,
                        cwd=cwd_hint,
                    )
                except Exception as e:
                    sys.stderr.write(
                        f"[polymem:codex] pending_observation failed for "
                        f"{path.name}: {e}\n"
                    )
        elif kind == ("event_msg", "user_message"):
            entry["lastUserMessage"] = payload.get("message") or ""
            entry["taskCompleteEmitted"] = False  # new turn = re-arm summary
        elif kind == ("event_msg", "agent_message"):
            entry["lastAgentMessage"] = payload.get("message") or ""
        elif kind == ("event_msg", "task_complete"):
            if (
                entry["lastUserMessage"]
                and entry["lastAgentMessage"]
                and not entry["taskCompleteEmitted"]
            ):
                try:
                    api.pending_summary(
                        memory_session_id=entry["memorySessionId"],
                        client="codex",
                        last_user_message=entry["lastUserMessage"],
                        last_assistant_message=entry["lastAgentMessage"],
                    )
                    entry["taskCompleteEmitted"] = True
                except Exception as e:
                    sys.stderr.write(
                        f"[polymem:codex] pending_summary failed for "
                        f"{path.name}: {e}\n"
                    )

    if entry is not None:
        entry["byteOffset"] = consumed_bytes


# ─── Tick + main loop ──────────────────────────────────────────────────────


def _tick(state: dict, api: PolyMemClient, backfill_days: int) -> None:
    cutoff = backfill_days * 86_400
    files = _find_session_files(cutoff)
    for f in files:
        try:
            _process_file(f, state, api)
        except Exception as e:
            sys.stderr.write(f"[polymem:codex] {f.name}: {type(e).__name__}: {e}\n")
    # GC: drop state entries for files no longer existing
    live = {str(f) for f in files}
    for k in list(state["files"].keys()):
        if k not in live and not Path(k).exists():
            state["files"].pop(k, None)
    _save_state(state)


def main(argv: list[str] | None = None) -> int:
    api = PolyMemClient()
    if not api.is_healthy():
        base = os.environ.get("POLYMEM_BASE_URL", "http://127.0.0.1:37700")
        sys.stderr.write(
            f"[polymem:codex] engine not reachable at {base}\n"
            "Start it with: polymem engine\n"
        )
        return 1

    poll_ms = DEFAULT_POLL_MS
    backfill_days = DEFAULT_BACKFILL_DAYS

    print(
        f"[polymem:codex] watcher started — polling every {poll_ms}ms, "
        f"backfill {backfill_days}d",
        flush=True,
    )
    print(f"[polymem:codex] state file: {STATE_PATH}", flush=True)
    print(f"[polymem:codex] sessions:   {SESSIONS_ROOT}", flush=True)

    state = _load_state()
    stopping = {"flag": False}

    def _shutdown(signum, _frame):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        print("\n[polymem:codex] shutting down...", flush=True)
        _save_state(state)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while not stopping["flag"]:
        try:
            _tick(state, api, backfill_days)
        except Exception as e:
            sys.stderr.write(f"[polymem:codex] tick error: {e}\n")
        time.sleep(poll_ms / 1000.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
