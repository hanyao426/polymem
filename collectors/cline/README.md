# Cline Collector (stub)

Cline / Roo Code extension skeleton. Similar structure to Cursor collector but Cline exposes more of its agent loop.

## Hook points available in Cline

Cline's VSCode extension fires events before/after each tool execution through its internal `ClineProvider`. We can:

1. **Wrap Cline's tool executor** — intercept `executeCommand`, `readFile`, `writeToFile` etc. at the Cline layer
2. **Tap the message stream** — Cline writes all messages to `.clinerules/` and the webview, both observable
3. **Use MCP** — Cline supports MCP servers, PolyMem can expose itself as one

## Minimum viable implementation

```typescript
import { PolyMemClient } from "../base/client";

// Option A: VSCode extension hosting Cline's events
export function registerPolyMemForCline(clineProvider: any, api: PolyMemClient) {
  clineProvider.on("tool:pre", async (ev: any) => { /* ... */ });
  clineProvider.on("tool:post", async (ev: any) => {
    await api.pendingObservation({
      memory_session_id: ev.sessionId,
      client: "cline",
      tool_name: ev.toolName,
      tool_input: JSON.stringify(ev.params),
      tool_response: ev.result,
      cwd: ev.cwd,
    });
  });
}
```

## Status

Not implemented. Placeholder for when Cline's public extension API stabilizes.
