/**
 * PolyMem MCP Server — unified read interface for all clients.
 *
 * Any MCP-capable client (Claude Code, Cursor, Windsurf, ChatGPT Desktop,
 * Gemini CLI, Cline, etc.) can connect to this server and gain access to
 * the full cross-client memory via 6 tools:
 *
 *   memory_search        — FTS5 + ChromaDB hybrid search (all clients)
 *   memory_timeline      — context window around an anchor
 *   memory_get           — batch fetch by IDs
 *   memory_context       — get $PMEM block for this client/project
 *   memory_kg_query      — knowledge graph (borrowed from MemPalace)
 *   memory_recall_full   — fetch raw conversation backup
 *
 * Runs as stdio MCP (launched by each client individually).
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const POLYMEM_BASE =
  process.env.POLYMEM_BASE_URL || "http://127.0.0.1:37700";

const server = new Server(
  { name: "polymem", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

// ─── Tool registry ─────────────────────────────────────────────────────────

const TOOLS = [
  {
    name: "memory_search",
    description:
      "Search cross-client memory (observations from Claude Code, Cursor, Cline, ChatGPT, etc.). Returns IDs + titles.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string" },
        project: { type: "string" },
        client: {
          type: "string",
          description: "Filter by client (claude_code/cursor/cline/chatgpt/...)",
        },
        type: {
          type: "string",
          enum: ["bugfix", "feature", "refactor", "change", "discovery", "decision"],
        },
        limit: { type: "number", default: 20 },
      },
      required: ["query"],
    },
  },
  {
    name: "memory_get",
    description: "Fetch full observation details by ID (batch).",
    inputSchema: {
      type: "object",
      properties: {
        ids: { type: "array", items: { type: "number" } },
      },
      required: ["ids"],
    },
  },
  {
    name: "memory_context",
    description:
      "Get $PMEM context block for injection. Shows recent observations across all clients.",
    inputSchema: {
      type: "object",
      properties: {
        project: { type: "string" },
        client: { type: "string" },
        max_obs: { type: "number", default: 50 },
      },
      required: ["project"],
    },
  },
  {
    name: "memory_kg_query",
    description:
      "Query the knowledge graph for entity relations (borrowed from MemPalace).",
    inputSchema: {
      type: "object",
      properties: {
        entity: { type: "string" },
        as_of: { type: "string", description: "ISO date for temporal filter" },
        direction: { type: "string", enum: ["outgoing", "incoming", "both"] },
      },
      required: ["entity"],
    },
  },
  {
    name: "memory_recall_full",
    description:
      "Fetch full-text conversation backup (raw messages, not extracted observations).",
    inputSchema: {
      type: "object",
      properties: {
        memory_session_id: { type: "string" },
        limit: { type: "number", default: 100 },
      },
      required: ["memory_session_id"],
    },
  },
];

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

// ─── Tool dispatch ─────────────────────────────────────────────────────────

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args = {} } = req.params;

  switch (name) {
    case "memory_search": {
      const q = new URLSearchParams({ query: args.query, limit: String(args.limit || 20) });
      if (args.project) q.set("project", args.project);
      if (args.client) q.set("client", args.client);
      if (args.type) q.set("type", args.type);
      const resp = await fetch(`${POLYMEM_BASE}/v1/search?${q}`);
      const data = await resp.json();
      return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
    }
    case "memory_get": {
      const results = await Promise.all(
        (args.ids as number[]).map((id) =>
          fetch(`${POLYMEM_BASE}/v1/observations/${id}`).then((r) => r.json())
        )
      );
      return { content: [{ type: "text", text: JSON.stringify(results, null, 2) }] };
    }
    case "memory_context": {
      const q = new URLSearchParams({ project: args.project });
      if (args.client) q.set("client", args.client);
      if (args.max_obs) q.set("max_obs", String(args.max_obs));
      const resp = await fetch(`${POLYMEM_BASE}/v1/context?${q}`);
      const data = await resp.json();
      return { content: [{ type: "text", text: data.context || "" }] };
    }
    case "memory_kg_query": {
      const q = new URLSearchParams({ entity: args.entity });
      if (args.as_of) q.set("as_of", args.as_of);
      if (args.direction) q.set("direction", args.direction);
      const resp = await fetch(`${POLYMEM_BASE}/v1/kg/query?${q}`);
      const data = await resp.json();
      return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
    }
    case "memory_recall_full": {
      const q = new URLSearchParams({ limit: String(args.limit || 100) });
      const resp = await fetch(
        `${POLYMEM_BASE}/v1/raw/session/${encodeURIComponent(args.memory_session_id)}?${q}`
      );
      const data = await resp.json();
      return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
    }
    default:
      throw new Error(`Unknown tool: ${name}`);
  }
});

// ─── Boot ──────────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((e) => {
  console.error(`[polymem-mcp] fatal: ${e?.message || e}`);
  process.exit(1);
});
