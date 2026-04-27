# Writing a New Collector

Adding a new client to PolyMem is a single file. Here's the recipe.

## 1. Create `collectors/<client>/` directory

```bash
mkdir -p collectors/my-client
```

## 2. Implement ICollector

```typescript
// collectors/my-client/index.ts
import { ICollector, ObservationEvent, SessionInitPayload, SummaryEvent } from "../base/collector";
import { PolyMemClient } from "../base/client";

export class MyClientCollector implements ICollector {
  readonly client = "my_client";
  private api = new PolyMemClient();
  private sessions = new Map<string, string>();

  async onSessionStart(p: SessionInitPayload): Promise<string> {
    const { memory_session_id } = await this.api.sessionInit(p);
    this.sessions.set(p.client_session_id, memory_session_id);
    return memory_session_id;
  }

  async onSessionEnd(memId: string) {
    await this.api.sessionComplete(memId);
  }

  async onToolCall(ev: ObservationEvent) {
    await this.api.pendingObservation(ev);
  }

  async onStop(ev: SummaryEvent) {
    await this.api.pendingSummary(ev);
  }
}
```

## 3. Hook it to your client's native events

Depends on the client. Examples:

### VSCode-based (Cursor, Cline, Windsurf)

```typescript
// Extension activate
export function activate(ctx: vscode.ExtensionContext) {
  const collector = new MyClientCollector();
  let memId: string;

  // Session boot
  (async () => {
    memId = await collector.onSessionStart({
      client: "my_client",
      client_session_id: crypto.randomUUID(),
      project: vscode.workspace.name || "default",
    });
  })();

  // File edit events
  ctx.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument(async (doc) => {
      await collector.onToolCall({
        memory_session_id: memId,
        client: "my_client",
        tool_name: "FileEdit",
        tool_input: JSON.stringify({ file: doc.fileName }),
        tool_response: doc.getText().slice(0, 8000),
      });
    })
  );
}
```

### MCP-based (ChatGPT Desktop, others without extension API)

```typescript
// MCP tool handler for a "remember" tool the model invokes
server.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name === "remember") {
    await collector.onToolCall({
      memory_session_id: currentMemId,
      client: "my_client",
      tool_name: req.params.arguments.tool_name,
      tool_input: req.params.arguments.input,
      tool_response: req.params.arguments.response,
    });
  }
});
```

### Command-line (Aider, Codex)

Wrap the CLI entry point, or register a post-commit git hook, or parse the client's log file tail.

## 4. Test

1. Start the engine: `./scripts/start-engine.sh`
2. Run your collector
3. Fire an event
4. Query: `curl http://localhost:37700/v1/search?query=test`
5. Check: `sqlite3 ~/.polymem/polymem.db "SELECT client, type, title FROM observations ORDER BY id DESC LIMIT 5"`

## What makes a good collector

| Do | Don't |
|----|-------|
| Non-blocking (POST and forget) | Sync waiting for extraction |
| Swallow errors gracefully (never break the host client) | Crash the host on engine errors |
| Include `cwd` when available | Lose project context |
| Pass `model` if you know it | Leave model field blank |
| Use the engine's existing `pending` endpoint | Implement your own LLM extraction |
| Re-use the shared `PolyMemClient` | Write your own HTTP wrapper |

## Coverage expectations

No collector is perfect. Grade your coverage honestly:

- **Full coverage (Claude Code, Cline):** hooks every tool call → structured observation per action
- **Good coverage (Cursor, Gemini CLI):** file saves + chat turns → mostly captures intent
- **Sparse coverage (ChatGPT Desktop):** only when model calls MCP → gaps between explicit "remember" calls
- **Derived coverage (Aider via git):** post-commit reconstruction → loses intermediate steps

Document your collector's coverage profile in its README so users know what's captured.
