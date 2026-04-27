#!/usr/bin/env node
/**
 * @ccc/polymem CLI dispatcher.
 *
 * Subcommands:
 *   polymem mcp                  — run MCP server (read interface, stdio)
 *   polymem codex [--background] — run Codex JSONL watcher (collection)
 *   polymem hook <event>         — run a single Claude Code hook event
 *                                  (one-shot, called by hooks.json)
 *   polymem install-claude-code [--hybrid|--with-injection]
 *   polymem uninstall-claude-code
 *   polymem engine [start]       — run the Python engine (delegates to start-engine.sh)
 *   polymem report [date] [project] [client]
 *   polymem doctor               — diagnose engine + hook + MCP setup
 *
 * Notes:
 *   - The Python engine is NOT bundled with this npm package. You install it
 *     separately via `pip install polymem-engine` (or build from this repo).
 *     Reason: pip + npm separation; the Python side is the heavyweight server,
 *     the npm side is just the per-tool adapters.
 *   - All TypeScript files are run via `tsx` (auto-installed as a dep).
 */

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve, join } from "node:path";
import { existsSync } from "node:fs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const PKG_ROOT = resolve(__dirname, "..");

function fail(msg, code = 1) {
  process.stderr.write(`[polymem] ${msg}\n`);
  process.exit(code);
}

function findTsx() {
  // Prefer the locally-bundled tsx so global installs don't need anything in PATH.
  const local = join(PKG_ROOT, "node_modules", ".bin", "tsx");
  if (existsSync(local)) return local;
  return "tsx"; // assume it's in PATH (works when tsx is hoisted)
}

function runTs(relPath, args = [], opts = {}) {
  const target = join(PKG_ROOT, relPath);
  if (!existsSync(target)) fail(`missing source file: ${target}`);
  const tsx = findTsx();
  const child = spawn(tsx, [target, ...args], {
    stdio: opts.stdio ?? "inherit",
    env: { ...process.env, POLYMEM_ROOT: PKG_ROOT, ...(opts.env || {}) },
  });
  child.on("exit", (code) => process.exit(code ?? 0));
}

function runShell(relScript, args = []) {
  const script = join(PKG_ROOT, relScript);
  if (!existsSync(script)) fail(`missing script: ${script}`);
  const child = spawn("bash", [script, ...args], {
    stdio: "inherit",
    env: { ...process.env, POLYMEM_ROOT: PKG_ROOT },
  });
  child.on("exit", (code) => process.exit(code ?? 0));
}

async function doctor() {
  const checks = [];
  const base = process.env.POLYMEM_BASE_URL || "http://127.0.0.1:37700";

  // 1. Engine
  try {
    const r = await fetch(`${base}/v1/health`, { signal: AbortSignal.timeout(2000) });
    const j = await r.json();
    checks.push(["engine reachable", j.status === "ok", `${base} → ${JSON.stringify(j)}`]);
  } catch (e) {
    checks.push(["engine reachable", false, `${base} → ${e.message}`]);
  }

  // 2. tsx
  checks.push(["tsx available", existsSync(findTsx()) || findTsx() === "tsx", findTsx()]);

  // 3. Required source files
  const required = [
    "mcp-server/server.ts",
    "collectors/claude-code/hook-handler.ts",
    "collectors/codex/watcher.ts",
  ];
  for (const f of required) {
    checks.push([`source: ${f}`, existsSync(join(PKG_ROOT, f)), join(PKG_ROOT, f)]);
  }

  // 4. Hooks installed?
  const settings = join(process.env.HOME || "", ".claude/settings.json");
  if (existsSync(settings)) {
    const txt = await import("node:fs").then((m) => m.readFileSync(settings, "utf-8"));
    const hasPolymem = txt.includes("polymem");
    checks.push(["claude code hooks installed", hasPolymem, settings]);
  } else {
    checks.push(["claude code hooks installed", false, "(no ~/.claude/settings.json yet)"]);
  }

  // 5. Codex MCP registered?
  const codexConfig = join(process.env.HOME || "", ".codex/config.toml");
  if (existsSync(codexConfig)) {
    const txt = await import("node:fs").then((m) => m.readFileSync(codexConfig, "utf-8"));
    checks.push(["codex MCP registered", txt.includes("[mcp_servers.polymem]"), codexConfig]);
  } else {
    checks.push(["codex MCP registered", false, "(no ~/.codex/config.toml; codex not installed?)"]);
  }

  // Print
  let allOk = true;
  for (const [name, ok, detail] of checks) {
    const mark = ok ? "✓" : "✗";
    if (!ok) allOk = false;
    process.stdout.write(`${mark} ${name.padEnd(34)} ${detail}\n`);
  }
  if (!allOk) process.exit(1);
}

function help() {
  process.stdout.write(
    `@ccc/polymem — cross-tool memory CLI

USAGE
  polymem <subcommand> [args...]

SUBCOMMANDS
  mcp                                    Run MCP server (stdio; called by clients)
  codex [--background]                   Run Codex JSONL watcher
  hook <event>                           Run one Claude Code hook event (internal)
  install-claude-code [--hybrid|--with-injection]
                                         Install Claude Code hooks into ~/.claude/settings.json
  uninstall-claude-code                  Remove polymem hooks
  engine [start]                         Start the Python engine (requires pip install)
  report [date] [project] [client]       Print today's daily report
  doctor                                 Diagnose engine + hooks + MCP setup
  --help, -h                             This help

ENV
  POLYMEM_BASE_URL    engine HTTP base, default http://127.0.0.1:37700
  POLYMEM_LITE_DAYS   SessionStart $PMEM lite window, default 3
  POLYMEM_LITE_MAX    SessionStart $PMEM lite max obs, default 30
  POLYMEM_CODEX_POLL_MS         codex watcher poll, default 30000
  POLYMEM_CODEX_BACKFILL_DAYS   codex backfill window, default 1

DOCS
  README.md  full how-to
  API.md     HTTP + MCP reference
`
  );
}

const [, , cmd, ...rest] = process.argv;

switch (cmd) {
  case "mcp":
    runTs("mcp-server/server.ts", rest);
    break;
  case "codex":
    runTs("collectors/codex/watcher.ts", rest);
    break;
  case "hook":
    runTs("collectors/claude-code/hook-handler.ts", rest);
    break;
  case "install-claude-code":
    runShell("scripts/install-claude-code.sh", rest);
    break;
  case "uninstall-claude-code":
    runShell("scripts/uninstall-claude-code.sh", rest);
    break;
  case "engine":
    runShell("scripts/start-engine.sh", rest);
    break;
  case "report":
    runShell("scripts/daily-report.sh", rest);
    break;
  case "doctor":
  case "--doctor":
    await doctor();
    break;
  case "help":
  case "--help":
  case "-h":
  case undefined:
    help();
    break;
  default:
    process.stderr.write(`unknown subcommand: ${cmd}\n\n`);
    help();
    process.exit(1);
}
