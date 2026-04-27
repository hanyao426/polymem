# ChatGPT Desktop Collector (MCP-based)

ChatGPT Desktop (late 2025+) supports MCP Connectors. This collector runs an MCP server that ChatGPT can invoke.

## Limitation

**ChatGPT only calls MCP tools when the model decides to.** There is no "PostToolUse" equivalent.
→ Coverage is sparser than Claude Code. Works best when user explicitly says "remember this" or
  CLAUDE.md-equivalent instructions tell ChatGPT to proactively log events.

## Implementation pattern

```typescript
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { PolyMemClient } from "../base/client";

const api = new PolyMemClient();
const server = new Server({ name: "polymem-chatgpt", version: "0.1.0" }, {});

server.setRequestHandler(/* tools/list */, async () => ({
  tools: [
    {
      name: "remember",
      description: "Store a structured memory observation for future sessions",
      inputSchema: { /* type, title, narrative, facts[] */ },
    },
    {
      name: "recall",
      description: "Search memory for related past work",
      inputSchema: { /* query */ },
    },
  ],
}));

server.setRequestHandler(/* tools/call */, async (req) => {
  if (req.params.name === "remember") {
    await api.direct/* ... */;
  }
});
```

## ChatGPT Desktop config

```json
{
  "mcpServers": {
    "polymem": {
      "command": "bun",
      "args": ["~/demo/polymem/collectors/chatgpt-mcp/index.ts"]
    }
  }
}
```

## Status

Not implemented. Placeholder.
