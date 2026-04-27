/**
 * HTTP client for PolyMem engine.
 * Thin wrapper around fetch — all collectors share this.
 */

const POLYMEM_BASE =
  process.env.POLYMEM_BASE_URL || "http://127.0.0.1:37700";

export class PolyMemClient {
  constructor(private base: string = POLYMEM_BASE) {}

  private async post<T>(path: string, body: unknown): Promise<T> {
    const resp = await fetch(`${this.base}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      throw new Error(`PolyMem ${path} failed: ${resp.status} ${await resp.text()}`);
    }
    return resp.json() as Promise<T>;
  }

  private async get<T>(path: string): Promise<T> {
    const resp = await fetch(`${this.base}${path}`);
    if (!resp.ok) {
      throw new Error(`PolyMem ${path} failed: ${resp.status}`);
    }
    return resp.json() as Promise<T>;
  }

  sessionInit(payload: {
    client: string;
    client_session_id: string;
    project: string;
    model?: string;
    user_prompt?: string;
  }): Promise<{ memory_session_id: string }> {
    return this.post("/v1/sessions/init", payload);
  }

  sessionComplete(memory_session_id: string, status = "completed"): Promise<{ ok: true }> {
    return this.post("/v1/sessions/complete", { memory_session_id, status });
  }

  /** Enqueue tool-call event for async LLM extraction. */
  pendingObservation(ev: {
    memory_session_id: string;
    client: string;
    model?: string;
    tool_name: string;
    tool_input: string;
    tool_response: string;
    cwd?: string;
    prompt_number?: number;
  }): Promise<{ pending_id: number }> {
    return this.post("/v1/observations/pending", ev);
  }

  /** Enqueue summary extraction at session stop. */
  pendingSummary(ev: {
    memory_session_id: string;
    client: string;
    last_user_message: string;
    last_assistant_message: string;
    prompt_number?: number;
  }): Promise<{ pending_id: number }> {
    return this.post("/v1/summaries/pending", ev);
  }

  /** Full-text backup (unstructured). */
  raw(msg: {
    memory_session_id: string;
    client: string;
    model?: string;
    role: string;
    content: string;
    tool_name?: string;
    tool_input?: string;
    tool_response?: string;
    prompt_number?: number;
  }): Promise<{ id: number }> {
    return this.post("/v1/raw", msg);
  }

  getContext(
    project: string,
    client?: string,
    opts?: { lite?: boolean; days?: number; max_obs?: number; show_summary?: boolean }
  ): Promise<{ context: string }> {
    const q = new URLSearchParams({ project });
    if (client) q.set("client", client);
    if (opts?.lite) q.set("lite", "true");
    if (opts?.days != null) q.set("days", String(opts.days));
    if (opts?.max_obs != null) q.set("max_obs", String(opts.max_obs));
    if (opts?.show_summary === false) q.set("show_summary", "false");
    return this.get(`/v1/context?${q}`);
  }

  isHealthy(): Promise<boolean> {
    return this.get<{ status: string }>("/v1/health")
      .then((r) => r.status === "ok")
      .catch(() => false);
  }
}
