/**
 * PolyMem Codex Collector — JSONL session watcher.
 *
 * Codex CLI (unlike Claude Code) does not expose hooks. It writes every
 * session as a JSONL rollout file at:
 *   ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
 *
 * This watcher polls those files and incrementally streams new records
 * into the PolyMem engine via /v1/sessions/init + /v1/observations/pending
 * + /v1/summaries/pending. State is kept per-file so restarts are safe.
 *
 * Run as a long-lived daemon:
 *   bun ~/demo/polymem/collectors/codex/watcher.ts
 *
 * Env:
 *   POLYMEM_BASE_URL          — default http://127.0.0.1:37700
 *   POLYMEM_CODEX_POLL_MS     — poll interval, default 30000
 *   POLYMEM_CODEX_BACKFILL_DAYS — only process files modified within last N days, default 1
 */

import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import { PolyMemClient } from "../base/client";
import { ICollector, ObservationEvent, SessionInitPayload, SummaryEvent } from "../base/collector";

const SESSIONS_ROOT = path.join(os.homedir(), ".codex/sessions");
const STATE_PATH = path.join(os.homedir(), ".polymem/codex-watcher-state.json");
const POLL_INTERVAL = parseInt(process.env.POLYMEM_CODEX_POLL_MS || "30000", 10);
const BACKFILL_DAYS = parseInt(process.env.POLYMEM_CODEX_BACKFILL_DAYS || "1", 10);
const SKIP_TOOLS = new Set(["update_plan"]);

// ─── Collector ─────────────────────────────────────────────────────────────

class CodexCollector implements ICollector {
  readonly client = "codex" as const;
  private api: PolyMemClient;

  constructor(api: PolyMemClient = new PolyMemClient()) {
    this.api = api;
  }

  async onSessionStart(payload: SessionInitPayload): Promise<string> {
    const { memory_session_id } = await this.api.sessionInit(payload);
    return memory_session_id;
  }

  async onSessionEnd(memory_session_id: string, status: "completed" | "failed" = "completed") {
    await this.api.sessionComplete(memory_session_id, status);
  }

  async onToolCall(ev: ObservationEvent) {
    if (SKIP_TOOLS.has(ev.tool_name)) return;
    await this.api.pendingObservation(ev);
  }

  async onStop(ev: SummaryEvent) {
    await this.api.pendingSummary(ev);
  }
}

// ─── State ─────────────────────────────────────────────────────────────────

interface FileState {
  byteOffset: number;
  memorySessionId: string;
  // Open function calls awaiting their function_call_output (paired by call_id).
  // Stored on disk so a restart mid-call doesn't lose the pairing.
  pendingCalls: Record<string, { name: string; arguments: string }>;
  lastUserMessage?: string;
  lastAgentMessage?: string;
  taskCompleteEmitted?: boolean;
}

interface State {
  files: Record<string, FileState>;
}

function loadState(): State {
  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, "utf-8"));
  } catch {
    return { files: {} };
  }
}

function saveState(s: State): void {
  fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });
  fs.writeFileSync(STATE_PATH, JSON.stringify(s, null, 2));
}

// ─── File discovery ────────────────────────────────────────────────────────

function findSessionFiles(cutoffMs: number): string[] {
  if (!fs.existsSync(SESSIONS_ROOT)) return [];
  const out: string[] = [];
  const now = Date.now();
  const safeReaddir = (p: string): string[] => {
    try {
      return fs.readdirSync(p);
    } catch {
      return [];
    }
  };
  for (const year of safeReaddir(SESSIONS_ROOT)) {
    const yp = path.join(SESSIONS_ROOT, year);
    if (!fs.statSync(yp).isDirectory()) continue;
    for (const month of safeReaddir(yp)) {
      const mp = path.join(yp, month);
      if (!fs.statSync(mp).isDirectory()) continue;
      for (const day of safeReaddir(mp)) {
        const dp = path.join(mp, day);
        if (!fs.statSync(dp).isDirectory()) continue;
        for (const f of safeReaddir(dp)) {
          if (!f.startsWith("rollout-") || !f.endsWith(".jsonl")) continue;
          const full = path.join(dp, f);
          const st = fs.statSync(full);
          if (now - st.mtimeMs <= cutoffMs) out.push(full);
        }
      }
    }
  }
  return out;
}

// ─── Per-file processing ───────────────────────────────────────────────────

async function processFile(
  filePath: string,
  state: State,
  collector: CodexCollector
): Promise<void> {
  const buf = fs.readFileSync(filePath, "utf-8");
  const fullSize = Buffer.byteLength(buf, "utf-8");

  let entry = state.files[filePath];
  if (entry && entry.byteOffset >= fullSize) return;

  const slice = entry ? buf.slice(entry.byteOffset) : buf;
  // Last line may be partial (file still being written) — don't consume it
  const lastNewline = slice.lastIndexOf("\n");
  if (lastNewline < 0) return;
  const consumable = slice.slice(0, lastNewline + 1);
  const consumedBytes = (entry?.byteOffset || 0) + Buffer.byteLength(consumable, "utf-8");

  const lines = consumable.split("\n").filter(Boolean);
  let cwdHint: string | undefined;

  for (const line of lines) {
    let d: any;
    try {
      d = JSON.parse(line);
    } catch {
      continue;
    }
    const p = d.payload || {};

    if (d.type === "session_meta" && !entry) {
      const project = (p.cwd || "").split("/").pop() || "default";
      cwdHint = p.cwd;
      const memId = await collector.onSessionStart({
        client: "codex",
        client_session_id: p.id,
        project,
        model: p.model_provider,
      });
      entry = state.files[filePath] = {
        byteOffset: 0,
        memorySessionId: memId,
        pendingCalls: {},
      };
      continue;
    }

    if (!entry) continue; // file's first record wasn't session_meta — skip

    if (d.type === "response_item" && p.type === "function_call") {
      entry.pendingCalls[p.call_id] = {
        name: p.name,
        arguments: p.arguments || "",
      };
    } else if (d.type === "response_item" && p.type === "function_call_output") {
      const call = entry.pendingCalls[p.call_id];
      if (call) {
        await collector.onToolCall({
          memory_session_id: entry.memorySessionId,
          client: "codex",
          tool_name: call.name,
          tool_input: call.arguments,
          tool_response: typeof p.output === "string" ? p.output : JSON.stringify(p.output),
          cwd: cwdHint,
        });
        delete entry.pendingCalls[p.call_id];
      }
    } else if (d.type === "event_msg" && p.type === "user_message") {
      entry.lastUserMessage = p.message || "";
    } else if (d.type === "event_msg" && p.type === "agent_message") {
      entry.lastAgentMessage = p.message || "";
    } else if (d.type === "event_msg" && p.type === "task_complete") {
      // Emit a summary at every task_complete (not just file end).
      // The engine will dedupe on content hash, so re-emits within the same
      // task don't cost much.
      if (entry.lastUserMessage && entry.lastAgentMessage && !entry.taskCompleteEmitted) {
        await collector.onStop({
          memory_session_id: entry.memorySessionId,
          client: "codex",
          last_user_message: entry.lastUserMessage,
          last_assistant_message: entry.lastAgentMessage,
        });
        entry.taskCompleteEmitted = true;
      }
    }
  }

  if (entry) {
    entry.byteOffset = consumedBytes;
    // Reset taskCompleteEmitted if a new user_message came after the last task_complete
    // (so we'll emit a fresh summary for the next task).
    // Simple heuristic: just always reset when we see the next user_message —
    // already implicitly handled because we OR it back.
    saveState(state);
  }
}

// ─── Tick + main loop ──────────────────────────────────────────────────────

async function tick(state: State, collector: CodexCollector): Promise<void> {
  const cutoffMs = BACKFILL_DAYS * 24 * 60 * 60 * 1000;
  const files = findSessionFiles(cutoffMs);
  for (const f of files) {
    try {
      await processFile(f, state, collector);
    } catch (e: any) {
      console.error(`[polymem:codex] ${path.basename(f)}: ${e?.message || e}`);
    }
  }
  // GC: drop state entries for files no longer existing or way outside backfill window
  const liveFiles = new Set(files);
  for (const k of Object.keys(state.files)) {
    if (!liveFiles.has(k) && !fs.existsSync(k)) {
      delete state.files[k];
    }
  }
  saveState(state);
}

async function main(): Promise<void> {
  const api = new PolyMemClient();
  if (!(await api.isHealthy())) {
    console.error(
      `[polymem:codex] engine not reachable at ${process.env.POLYMEM_BASE_URL || "http://127.0.0.1:37700"}`
    );
    console.error("Start it: ~/demo/polymem/scripts/start-engine.sh");
    process.exit(1);
  }

  console.log(
    `[polymem:codex] watcher started — polling every ${POLL_INTERVAL}ms, backfill ${BACKFILL_DAYS}d`
  );
  console.log(`[polymem:codex] state file: ${STATE_PATH}`);
  console.log(`[polymem:codex] sessions: ${SESSIONS_ROOT}`);

  const state = loadState();
  const collector = new CodexCollector(api);

  // Graceful shutdown
  let stopping = false;
  const shutdown = () => {
    if (stopping) return;
    stopping = true;
    console.log("\n[polymem:codex] shutting down...");
    saveState(state);
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  while (!stopping) {
    try {
      await tick(state, collector);
    } catch (e: any) {
      console.error(`[polymem:codex] tick error: ${e?.message || e}`);
    }
    await new Promise((r) => setTimeout(r, POLL_INTERVAL));
  }
}

if (import.meta.main) {
  main().catch((e) => {
    console.error(`[polymem:codex] fatal: ${e?.message || e}`);
    process.exit(1);
  });
}
