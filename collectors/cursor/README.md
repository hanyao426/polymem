# Cursor Collector (stub)

Cursor extension skeleton. Implements `ICollector` using Cursor's VSCode-compatible extension API.

## Hook points available in Cursor

- `vscode.workspace.onDidSaveTextDocument` — file edits
- `vscode.commands.executeCommand("cursor.chat.submit")` — can wrap to capture chat turns
- MCP server (Cursor 0.42+) — can receive tool calls via MCP

## Minimum viable implementation

```typescript
import { PolyMemClient } from "../base/client";
import * as vscode from "vscode";

export function activate(ctx: vscode.ExtensionContext) {
  const api = new PolyMemClient();
  const sessionId = crypto.randomUUID();
  let memId: string;

  (async () => {
    const { memory_session_id } = await api.sessionInit({
      client: "cursor",
      client_session_id: sessionId,
      project: vscode.workspace.name || "default",
    });
    memId = memory_session_id;
  })();

  ctx.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument(async (doc) => {
      await api.pendingObservation({
        memory_session_id: memId,
        client: "cursor",
        tool_name: "FileEdit",
        tool_input: JSON.stringify({ file: doc.fileName }),
        tool_response: doc.getText().slice(0, 8000),
        cwd: vscode.workspace.workspaceFolders?.[0].uri.fsPath,
      });
    })
  );
}
```

## Limitations vs Claude Code

Cursor doesn't expose Claude Code's PostToolUse granularity. We can only hook:
- File saves (via VSCode API)
- Chat turns (via wrapping command)
- MCP tool calls (if user runs MCP server)

Tool call input/response visibility is lower → expect sparser observations than Claude Code.
