"""Install / uninstall PolyMem hooks into Claude Code's settings.json.

Pure-Python replacement for the bash + jq install script. Behavior:
  - Backs up settings.json before any change (.bak.YYYYMMDD-HHMMSS)
  - APPENDS to existing hook arrays (does NOT overwrite user's other hooks)
  - Idempotent: refuses to install twice; uninstall removes only polymem entries
  - Detects polymem absolute path via shutil.which() so PATH doesn't matter
    when Claude Code spawns the hook subprocess
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any


SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
TEMPLATE_BY_MODE = {
    "collect-only": "claude_code_collect_only.json",
    "hybrid": "claude_code_hybrid.json",
    "full-injection": "claude_code_full_injection.json",
}


def _load_settings() -> dict:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except json.JSONDecodeError as e:
        sys.stderr.write(
            f"settings.json invalid JSON: {e}. Refusing to overwrite.\n"
        )
        sys.exit(1)


def _save_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False))


def _backup() -> Path:
    if not SETTINGS_PATH.exists():
        SETTINGS_PATH.write_text("{}\n")
    backup = SETTINGS_PATH.with_suffix(
        f".json.bak.{time.strftime('%Y%m%d-%H%M%S')}"
    )
    backup.write_bytes(SETTINGS_PATH.read_bytes())
    return backup


def _polymem_bin() -> str:
    """Return absolute path to the polymem executable (or 'polymem' if not found)."""
    found = shutil.which("polymem")
    if found:
        return found
    sys.stderr.write(
        "warning: 'polymem' command not found in PATH. Hooks will use the bare name.\n"
        "        After pipx install, ensure ~/.local/bin is in PATH.\n"
    )
    return "polymem"


def _has_polymem_hooks(settings: dict) -> bool:
    """Detect existing polymem hook entries (any nested command containing 'polymem hook')."""
    def walk(o: Any) -> bool:
        if isinstance(o, dict):
            cmd = o.get("command")
            if isinstance(cmd, str) and "polymem" in cmd.lower():
                return True
            return any(walk(v) for v in o.values())
        if isinstance(o, list):
            return any(walk(v) for v in o)
        return False
    return walk(settings.get("hooks") or {})


def _load_template(mode: str) -> dict:
    name = TEMPLATE_BY_MODE.get(mode)
    if not name:
        sys.stderr.write(
            f"unknown mode: {mode}. Choose: " + ", ".join(TEMPLATE_BY_MODE) + "\n"
        )
        sys.exit(2)
    raw = (HOOKS_DIR / name).read_text()
    raw = raw.replace("{{POLYMEM_BIN}}", _polymem_bin())
    return json.loads(raw)


def _merge_hooks(settings: dict, template: dict) -> None:
    """Append template hooks to settings, never replacing existing arrays."""
    settings.setdefault("hooks", {})
    for event, blocks in (template.get("hooks") or {}).items():
        existing = settings["hooks"].setdefault(event, [])
        existing.extend(blocks)


def cmd_install(args: argparse.Namespace) -> int:
    mode: str = args.mode
    settings = _load_settings()
    if _has_polymem_hooks(settings):
        sys.stderr.write(
            "PolyMem hooks already installed. Run 'polymem uninstall-claude-code' first.\n"
        )
        return 1
    backup = _backup()
    print(f"✓ Backup: {backup}")

    template = _load_template(mode)
    _merge_hooks(settings, template)
    _save_settings(settings)

    print(f"✓ Hooks merged (mode={mode}) into {SETTINGS_PATH}")
    print()
    print("Hook events affected:")
    for event, blocks in (settings.get("hooks") or {}).items():
        print(f"  {event}: {len(blocks)} total hook-block(s)")
    print()
    print(f"polymem command resolved to: {_polymem_bin()}")
    print()
    print("Next steps:")
    print("  1. Open a NEW Claude Code session (hooks don't hot-reload).")
    if mode == "hybrid":
        print("  2. Look for: '[PolyMem] injected $PMEM lite index ...' on session start.")
    elif mode == "full-injection":
        print("  2. Look for the full $PMEM block injected at session start.")
    print("  3. Add this to settings.json mcpServers for read access:")
    print('       { "polymem": { "command": "polymem", "args": ["mcp"] } }')
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not SETTINGS_PATH.exists():
        print(f"settings.json not found: {SETTINGS_PATH}")
        return 0
    settings = _load_settings()
    backup = _backup()
    print(f"✓ Backup: {backup}")

    hooks = settings.get("hooks") or {}
    new_hooks: dict[str, list] = {}
    for event, blocks in hooks.items():
        kept_blocks: list = []
        for block in blocks:
            inner = []
            for h in (block.get("hooks") or []):
                if isinstance(h.get("command"), str) and "polymem" in h["command"].lower():
                    continue
                inner.append(h)
            if inner:
                # If we removed some but not all, keep the block with remaining hooks
                new_block = dict(block)
                new_block["hooks"] = inner
                kept_blocks.append(new_block)
        if kept_blocks:
            new_hooks[event] = kept_blocks

    settings["hooks"] = new_hooks
    _save_settings(settings)

    print("✓ PolyMem hooks removed. Remaining events:")
    if not new_hooks:
        print("  (none)")
    else:
        for event, blocks in new_hooks.items():
            print(f"  {event}: {len(blocks)} hook-block(s)")
    return 0


# ─── Codex install: append [mcp_servers.polymem] to ~/.codex/config.toml ──


CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
CODEX_AGENTS_MD = Path.home() / ".codex" / "AGENTS.md"
CODEX_MCP_BLOCK = """\

[mcp_servers.polymem]
type = "stdio"
command = "{bin}"
args = ["mcp"]
"""

CODEX_AGENTS_RULE = """\
## 记忆系统使用规则（PolyMem）

接入 PolyMem MCP（`polymem`），跨工具共享记忆——既包括 Codex 自己产生的，也包括用户在 Claude Code 里产生的。

### 何时主动查记忆

满足以下**任一**条件就先调 `memory_search`，再开始动手：

- 用户提到"上次/之前/继续"等指向过去的词
- 任务涉及已存在的代码模块（不是从零新建）
- 用户描述的问题听起来已经讨论或修过
- 用户提到具体文件路径但你不知道历史

### 执行模式

1. 第一轮：`memory_search` 找相关 ID（5-10 条够用）
2. 第二轮：`memory_get` 拿真正相关的 2-3 条详情
3. 再开始动手——不要在没确认历史的情况下重复造轮子

### 跨客户端识别

每条 observation 带 `client` 字段（`claude_code` / `codex` / 等）。引用时注明来源："你之前在 Claude Code 里把 X 重构过（#500），现在我们..."。

### 失败兜底

`memory_search` 报错或空结果时，告知用户"PolyMem 暂时查不到相关历史"后继续，不要阻塞。
"""


def cmd_install_codex(args: argparse.Namespace) -> int:
    if not CODEX_CONFIG.exists():
        sys.stderr.write(
            f"codex config not found: {CODEX_CONFIG}\n"
            "Install Codex CLI first.\n"
        )
        return 1
    text = CODEX_CONFIG.read_text()
    if "[mcp_servers.polymem]" in text:
        print("⚠ polymem MCP already registered in codex config — skipping")
    else:
        backup = CODEX_CONFIG.with_suffix(
            f".toml.bak.{time.strftime('%Y%m%d-%H%M%S')}"
        )
        backup.write_text(text)
        print(f"✓ Backup: {backup}")
        CODEX_CONFIG.write_text(text + CODEX_MCP_BLOCK.format(bin=_polymem_bin()))
        print(f"✓ Registered polymem MCP in {CODEX_CONFIG}")

    if CODEX_AGENTS_MD.exists():
        existing = CODEX_AGENTS_MD.read_text()
    else:
        existing = ""
    if "PolyMem" in existing:
        print("⚠ AGENTS.md already mentions PolyMem — skipping rule append")
    else:
        CODEX_AGENTS_MD.parent.mkdir(parents=True, exist_ok=True)
        CODEX_AGENTS_MD.write_text(
            (existing + "\n\n" if existing.strip() else "")
            + CODEX_AGENTS_RULE
        )
        print(f"✓ Appended memory-usage rule to {CODEX_AGENTS_MD}")

    print()
    print("Next: restart Codex (MCP servers load at startup).")
    print("Then start the codex JSONL watcher in background:")
    print("  nohup polymem codex > ~/.polymem/codex-watcher.log 2>&1 &")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polymem install")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cc = sub.add_parser("claude-code", help="Install Claude Code hooks")
    p_cc.add_argument(
        "--mode",
        choices=list(TEMPLATE_BY_MODE.keys()),
        default="hybrid",
        help="hook injection level (default: hybrid)",
    )
    p_cc.set_defaults(func=cmd_install)

    p_un = sub.add_parser("uninstall-claude-code", help="Remove Claude Code hooks")
    p_un.set_defaults(func=cmd_uninstall)

    p_cx = sub.add_parser("codex", help="Register polymem MCP + AGENTS.md rule for Codex")
    p_cx.set_defaults(func=cmd_install_codex)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
