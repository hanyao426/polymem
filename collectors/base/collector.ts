/**
 * PolyMem Collector Base Interface.
 *
 * Every client (Claude Code, Cursor, Cline, ChatGPT, Aider, ...) implements
 * this interface. Collectors are thin — they capture events from their host
 * and forward them to the PolyMem engine HTTP API. The engine does all
 * heavy lifting: LLM extraction, storage, search, context generation.
 *
 * Extensibility:
 *   To add a new client, write one file: `collectors/<client>/index.ts`
 *   that implements ICollector. You never need to touch the engine.
 */

export type ClientName =
  | "claude_code"
  | "cursor"
  | "cline"
  | "windsurf"
  | "gemini_cli"
  | "chatgpt"
  | "aider"
  | "codex"
  | string; // extensible — new clients just use their own string

export interface SessionInitPayload {
  client: ClientName;
  client_session_id: string;
  project: string;
  model?: string;
  user_prompt?: string;
}

export interface ObservationEvent {
  memory_session_id: string;
  client: ClientName;
  model?: string;
  tool_name: string;
  tool_input: string;
  tool_response: string;
  cwd?: string;
  prompt_number?: number;
}

export interface SummaryEvent {
  memory_session_id: string;
  client: ClientName;
  last_user_message: string;
  last_assistant_message: string;
  prompt_number?: number;
}

export interface RawMessage {
  memory_session_id: string;
  client: ClientName;
  model?: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  tool_name?: string;
  tool_input?: string;
  tool_response?: string;
  prompt_number?: number;
}

/**
 * Base interface every collector implements.
 * Lifecycle mirrors Claude Code's hook events, adapted as needed per client.
 */
export interface ICollector {
  readonly client: ClientName;

  /** Called when a new session starts. Returns the memory_session_id. */
  onSessionStart(payload: SessionInitPayload): Promise<string>;

  /** Called when a session ends. */
  onSessionEnd(memory_session_id: string, status?: "completed" | "failed"): Promise<void>;

  /**
   * Called after every tool/action the model takes. This is the single most
   * important hook — it's where the bulk of memory is captured.
   */
  onToolCall(ev: ObservationEvent): Promise<void>;

  /** Called at end of a user-assistant exchange. Triggers summary extraction. */
  onStop(ev: SummaryEvent): Promise<void>;

  /** Optional: mirror raw user/assistant messages for full-text backup. */
  onRawMessage?(msg: RawMessage): Promise<void>;

  /**
   * Optional: retrieve context block for injection at session start.
   * Claude Code Hooks can return this as hookSpecificOutput.
   */
  getContext?(project: string): Promise<string>;
}
