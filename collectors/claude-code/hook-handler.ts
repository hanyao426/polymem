/**
 * PolyMem Claude Code Collector.
 *
 * Implements ICollector by consuming Claude Code's hook stdin JSON.
 * Dispatched by hooks.json via bun-runner.js (copied from claude-mem).
 *
 * Usage (from hooks.json):
 *   PostToolUse → bun hook-handler.ts observation
 *   UserPromptSubmit → bun hook-handler.ts session-init
 *   SessionStart → bun hook-handler.ts context        (full $PMEM)
 *   SessionStart → bun hook-handler.ts context-lite   (lightweight index, hybrid mode)
 *   Stop → bun hook-handler.ts summarize
 *   SessionEnd → bun hook-handler.ts session-complete
 */

import { PolyMemClient } from "../base/client";
import { ICollector } from "../base/collector";
import type {
  ObservationEvent,
  SessionInitPayload,
  SummaryEvent,
  RawMessage,
} from "../base/collector";

const SKIP_TOOLS = new Set([
  "ListMcpResourcesTool",
  "SlashCommand",
  "Skill",
  "TodoWrite",
  "AskUserQuestion",
]);

export class ClaudeCodeCollector implements ICollector {
  readonly client = "claude_code" as const;
  private api: PolyMemClient;
  private sessionMap = new Map<string, string>(); // client_session_id → memory_session_id

  constructor(api: PolyMemClient = new PolyMemClient()) {
    this.api = api;
  }

  async onSessionStart(payload: SessionInitPayload): Promise<string> {
    const { memory_session_id } = await this.api.sessionInit(payload);
    this.sessionMap.set(payload.client_session_id, memory_session_id);
    return memory_session_id;
  }

  async onSessionEnd(memory_session_id: string, status = "completed") {
    await this.api.sessionComplete(memory_session_id, status as "completed" | "failed");
  }

  async onToolCall(ev: ObservationEvent) {
    if (SKIP_TOOLS.has(ev.tool_name)) return;
    await this.api.pendingObservation(ev);
  }

  async onStop(ev: SummaryEvent) {
    await this.api.pendingSummary(ev);
  }

  async onRawMessage(msg: RawMessage) {
    await this.api.raw(msg);
  }

  async getContext(
    project: string,
    opts?: { lite?: boolean; days?: number; max_obs?: number }
  ): Promise<string> {
    const { context } = await this.api.getContext(project, this.client, opts);
    return context;
  }
}

// ─── Hook dispatch (CLI entry point) ──────────────────────────────────────

async function readStdin(): Promise<string> {
  const chunks: Uint8Array[] = [];
  for await (const chunk of Bun.stdin.stream()) chunks.push(chunk);
  return Buffer.concat(chunks).toString();
}

async function main() {
  const cmd = process.argv[2];
  if (!cmd) {
    console.error("usage: hook-handler.ts <observation|session-init|context|summarize|session-complete>");
    process.exit(1);
  }

  const raw = await readStdin().catch(() => "");
  const hookInput = raw ? JSON.parse(raw) : {};

  const collector = new ClaudeCodeCollector();
  const clientSessionId: string = hookInput.session_id || "unknown";

  // Resolve memory_session_id (create if first event in session)
  let memId: string;
  const cached = collector["sessionMap"].get(clientSessionId);
  if (cached) {
    memId = cached;
  } else {
    const project =
      hookInput.cwd?.split("/").pop() || process.env.POLYMEM_PROJECT || "default";
    memId = await collector.onSessionStart({
      client: "claude_code",
      client_session_id: clientSessionId,
      project,
      model: hookInput.model,
      user_prompt: hookInput.prompt,
    });
  }

  switch (cmd) {
    case "context": {
      const project = hookInput.cwd?.split("/").pop() || "default";
      const context = await collector.getContext(project);
      console.log(
        JSON.stringify({
          continue: true,
          hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: context },
        })
      );
      break;
    }
    case "context-lite": {
      // Hybrid mode: inject a lightweight index (titles only, last N days),
      // letting the model fetch details via MCP tools on demand.
      const project = hookInput.cwd?.split("/").pop() || "default";
      const days = Number(process.env.POLYMEM_LITE_DAYS || 3);
      const maxObs = Number(process.env.POLYMEM_LITE_MAX || 30);
      const context = await collector.getContext(project, {
        lite: true,
        days,
        max_obs: maxObs,
      });
      // Approximate observation count by counting list entries (lines starting with a digit).
      const obsCount = (context.match(/^\d+\s+\d/gm) || []).length;
      const tokenEstimate = Math.round(context.length / 4);
      console.log(
        JSON.stringify({
          continue: true,
          systemMessage: `[PolyMem] injected $PMEM lite index — ${obsCount} obs, ~${tokenEstimate} tokens, last ${days}d (project=${project}). Use MCP memory_search/memory_get for details.`,
          hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: context },
        })
      );
      break;
    }
    case "session-init": {
      // UserPromptSubmit — just ensure session exists
      console.log(JSON.stringify({ continue: true, suppressOutput: true }));
      break;
    }
    case "observation": {
      await collector.onToolCall({
        memory_session_id: memId,
        client: "claude_code",
        tool_name: hookInput.tool_name || "",
        tool_input: JSON.stringify(hookInput.tool_input || {}),
        tool_response: JSON.stringify(hookInput.tool_response || {}),
        cwd: hookInput.cwd,
      });
      console.log(JSON.stringify({ continue: true, suppressOutput: true }));
      break;
    }
    case "summarize": {
      await collector.onStop({
        memory_session_id: memId,
        client: "claude_code",
        last_user_message: hookInput.last_user_message || "",
        last_assistant_message: hookInput.last_assistant_message || "",
      });
      console.log(JSON.stringify({ continue: true, suppressOutput: true }));
      break;
    }
    case "session-complete": {
      await collector.onSessionEnd(memId);
      console.log(JSON.stringify({ continue: true, suppressOutput: true }));
      break;
    }
    default:
      console.error(`unknown cmd: ${cmd}`);
      process.exit(1);
  }
}

if (import.meta.main) {
  main().catch((e) => {
    // Never fail hard — collector errors must not block Claude Code
    console.error(`[polymem:claude-code] ${e?.message || e}`);
    console.log(JSON.stringify({ continue: true, suppressOutput: true }));
    process.exit(0);
  });
}
