"""Top-level `polymem` CLI dispatcher.

Subcommands:
  polymem engine                     start the FastAPI engine
  polymem mcp                        run MCP stdio server (called by clients)
  polymem codex                      run Codex JSONL watcher (collection)
  polymem hook <event>               run one Claude Code hook event (internal)
  polymem install claude-code [--mode hybrid|collect-only|full-injection]
  polymem install uninstall-claude-code
  polymem install codex              register polymem MCP + AGENTS.md rule for Codex
  polymem init                       interactive bootstrap: detect clients + configure all
  polymem doctor                     health check
  polymem report [date] [project] [client]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _help() -> None:
    sys.stdout.write(__doc__ or "polymem\n")


def _cmd_engine(args: list[str]) -> int:
    """Start the FastAPI engine in foreground."""
    from polymem.server import main as engine_main
    os.environ.setdefault("POLYMEM_PROVIDER", "claude_cli")
    os.environ.setdefault("POLYMEM_MODEL", "claude-haiku-4-5-20251001")
    return engine_main() or 0


def _cmd_mcp(args: list[str]) -> int:
    from polymem.cli.mcp_server import main as mcp_main
    return mcp_main(args)


def _cmd_codex(args: list[str]) -> int:
    from polymem.cli.codex_watcher import main as watcher_main
    return watcher_main(args)


def _cmd_hook(args: list[str]) -> int:
    from polymem.cli.hook import main as hook_main
    return hook_main(args)


def _cmd_install(args: list[str]) -> int:
    from polymem.cli.install import main as install_main
    return install_main(args)


def _cmd_report(args: list[str]) -> int:
    from polymem.cli.client import PolyMemClient
    api = PolyMemClient()
    date = args[0] if len(args) > 0 and args[0] else None
    project = args[1] if len(args) > 1 and args[1] else None
    client = args[2] if len(args) > 2 and args[2] else None
    try:
        result = api.report(date=date, project=project, client=client)
    except Exception as e:
        sys.stderr.write(f"engine not reachable: {e}\n")
        return 1
    print(result.get("report") or result)
    return 0


def _cmd_doctor(args: list[str]) -> int:
    from polymem.cli.client import PolyMemClient
    checks: list[tuple[str, bool, str]] = []
    api = PolyMemClient()
    base = api.base
    try:
        ok = api.is_healthy()
        checks.append(("engine reachable", ok, f"{base}"))
    except Exception as e:
        checks.append(("engine reachable", False, f"{base} → {e}"))

    polymem_bin = shutil.which("polymem")
    checks.append(("polymem in PATH", bool(polymem_bin), polymem_bin or "(not found)"))

    settings = Path.home() / ".claude" / "settings.json"
    has_cc_hooks = False
    if settings.exists():
        text = settings.read_text(errors="ignore")
        has_cc_hooks = "polymem hook" in text
    checks.append((
        "claude code hooks installed",
        has_cc_hooks,
        str(settings) if settings.exists() else "(no settings.json)",
    ))

    cc_mcp = False
    if settings.exists():
        try:
            import json
            data = json.loads(settings.read_text())
            cc_mcp = "polymem" in (data.get("mcpServers") or {})
        except Exception:
            pass
    checks.append((
        "claude code MCP registered",
        cc_mcp,
        "settings.json mcpServers.polymem",
    ))

    codex_cfg = Path.home() / ".codex" / "config.toml"
    has_codex_mcp = codex_cfg.exists() and "[mcp_servers.polymem]" in codex_cfg.read_text(errors="ignore")
    checks.append((
        "codex MCP registered",
        has_codex_mcp,
        str(codex_cfg) if codex_cfg.exists() else "(no codex config; not installed?)",
    ))

    codex_agents = Path.home() / ".codex" / "AGENTS.md"
    has_agents_rule = codex_agents.exists() and "PolyMem" in codex_agents.read_text(errors="ignore")
    checks.append((
        "codex AGENTS.md rule",
        has_agents_rule,
        str(codex_agents) if codex_agents.exists() else "(no AGENTS.md)",
    ))

    all_ok = True
    for name, ok, detail in checks:
        mark = "✓" if ok else "✗"
        if not ok:
            all_ok = False
        print(f"{mark} {name:<32}  {detail}")
    return 0 if all_ok else 1


def _cmd_init(args: list[str]) -> int:
    """Interactive (or with --yes auto) bootstrap.

    1. Verify engine reachable; if not, suggest starting it.
    2. Detect Claude Code → offer to install hybrid hooks + register MCP.
    3. Detect Codex → offer to register MCP + AGENTS.md.
    4. Print next-step pointers.
    """
    yes = "--yes" in args or "-y" in args
    print("=== PolyMem init ===\n")

    from polymem.cli.client import PolyMemClient
    api = PolyMemClient()
    if not api.is_healthy():
        print("✗ Engine not running.")
        print("  Start it in another shell:  polymem engine")
        print("  Or backgrounded:            nohup polymem engine > ~/.polymem/engine.log 2>&1 &")
        print("  Then re-run: polymem init")
        return 1
    print(f"✓ Engine healthy at {api.base}\n")

    # Claude Code
    cc_settings = Path.home() / ".claude" / "settings.json"
    if cc_settings.exists():
        print(f"✓ Detected Claude Code at {cc_settings}")
        text = cc_settings.read_text(errors="ignore")
        if "polymem hook" in text:
            print("  → hooks already installed, skipping")
        else:
            ans = "y" if yes else input(
                "  Install hybrid-mode hooks (recommended)? [Y/n] "
            ).strip().lower()
            if ans in ("", "y", "yes"):
                from polymem.cli.install import main as install_main
                install_main(["claude-code", "--mode", "hybrid"])
                print()

        # MCP register
        try:
            import json
            data = json.loads(cc_settings.read_text())
            if "polymem" in (data.get("mcpServers") or {}):
                print("  → MCP already registered, skipping")
            else:
                ans = "y" if yes else input(
                    "  Register polymem MCP in settings.json? [Y/n] "
                ).strip().lower()
                if ans in ("", "y", "yes"):
                    data.setdefault("mcpServers", {})
                    data["mcpServers"]["polymem"] = {
                        "command": shutil.which("polymem") or "polymem",
                        "args": ["mcp"],
                    }
                    cc_settings.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                    print("  ✓ MCP registered")
        except Exception as e:
            print(f"  ⚠ couldn't update MCP entry: {e}")
        print()
    else:
        print("(no Claude Code detected)\n")

    # Codex
    codex_cfg = Path.home() / ".codex" / "config.toml"
    if codex_cfg.exists():
        print(f"✓ Detected Codex at {codex_cfg}")
        ans = "y" if yes else input(
            "  Register MCP + write AGENTS.md memory rule? [Y/n] "
        ).strip().lower()
        if ans in ("", "y", "yes"):
            from polymem.cli.install import main as install_main
            install_main(["codex"])
        print()
    else:
        print("(no Codex detected)\n")

    print("All done. Next steps:")
    print("  • Open a NEW Claude Code or Codex session — hooks/MCP load at startup.")
    print("  • For Codex collection: nohup polymem codex > ~/.polymem/codex-watcher.log 2>&1 &")
    print("  • Verify: polymem doctor")
    return 0


COMMANDS = {
    "engine": _cmd_engine,
    "mcp": _cmd_mcp,
    "codex": _cmd_codex,
    "hook": _cmd_hook,
    "install": _cmd_install,
    "init": _cmd_init,
    "doctor": _cmd_doctor,
    "report": _cmd_report,
}


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help", "help"):
        _help()
        return 0
    cmd, rest = args[0], args[1:]
    handler = COMMANDS.get(cmd)
    if not handler:
        sys.stderr.write(f"unknown subcommand: {cmd}\n\n")
        _help()
        return 1
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main())
