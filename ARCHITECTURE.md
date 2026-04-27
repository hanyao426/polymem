# PolyMem Architecture

## Design goals

1. **Auto-collection across all major AI coding clients** — no model should be left behind
2. **Reuse proven parts of claude-mem and MemPalace** — don't reinvent working components
3. **Extensibility as first-class concern** — adding a new client = one file, zero engine changes
4. **Five capabilities of the "ideal memory system"**:
   - ✅ Auto-collection → Collectors (per-client hooks)
   - ✅ Structured extraction → LLM-based XML extraction (claude-mem's proven prompt)
   - ✅ Full-text backup → `raw_conversations` table (MemPalace-style)
   - ✅ Vector semantic search → ChromaDB + BM25 rerank (MemPalace's searcher)
   - ✅ Knowledge graph → SQLite temporal triples (MemPalace's KG)

## Why Collector pattern

The fundamental challenge: every AI client has different extension mechanisms.

| Client | Hook mechanism | Coverage |
|--------|---------------|----------|
| Claude Code | Hooks (PostToolUse/Stop/SessionStart) | **Full** — sees every tool call |
| Cursor | VSCode API + MCP | Partial — file saves + MCP calls |
| Cline / Roo Code | Internal events + MCP | Good — tool executor hookable |
| Windsurf | MCP + `.windsurfrules` | Partial |
| Gemini CLI | MCP + extensions | Good |
| ChatGPT Desktop | MCP Connectors | Sparse — only when model calls MCP |
| Aider | Plugin system | Good — git-level events |

**The Collector pattern isolates this variability.** The engine speaks one protocol (HTTP POST).
Each collector speaks two: its client's native protocol on one side, engine HTTP on the other.

The `ICollector` interface (`collectors/base/collector.ts`) defines the 5 lifecycle events every
collector should implement:

```typescript
onSessionStart()  // create memory_session_id
onToolCall()      // enqueue observation (the core event)
onStop()          // enqueue summary
onSessionEnd()    // close session
onRawMessage()    // optional: mirror raw messages for full-text backup
getContext()      // optional: fetch $PMEM for session-start injection
```

New clients implement this interface using whatever hooks they have available. The engine never changes.

## Data flow: PostToolUse (Claude Code example)

```
1. User asks Claude to read a file
2. Claude invokes Read tool
3. Claude Code fires PostToolUse hook
   └─ invokes: bun hook-handler.ts observation
4. hook-handler.ts reads stdin {session_id, tool_name, tool_input, tool_response, cwd}
5. ClaudeCodeCollector.onToolCall(event)
   └─ POST http://localhost:37700/v1/observations/pending
6. Engine writes to pending_messages table (returns immediately — non-blocking)
7. Claude Code continues normally
8. ExtractionWorker (background thread) claims pending row
   └─ Calls LLM with mode/code.json prompt
   └─ Parses <observation> XML
   └─ content_hash dedup check (30s window)
   └─ INSERT INTO observations (triggers FTS5 maintenance)
```

Key decisions:
- **Async extraction** — the hook returns in < 50ms. LLM call happens in background.
- **content_hash dedup** — if two collectors independently log the same event (e.g., correlation failure), dedup catches it.
- **Per-client field** — every observation carries its `client` name, enabling cross-client search, per-client filters, etc.

## Why async extraction, not sync?

Claude Code's PostToolUse hook has a 120s timeout. If every tool call triggered a synchronous LLM
call, typical hook latency would be ~2-8s, making Claude Code sluggish. With async:

- Hook latency: < 50ms (just HTTP POST)
- Extraction latency: 2-8s in background, completes by the time user looks at the result

## Why reuse both projects instead of one?

**claude-mem solves write-side automation** — hooks, LLM extraction, structured observations.
Its retrieval is good but built on FTS5 + optional ChromaDB.

**MemPalace solves read-side retrieval** — two-layer retrieval, BM25 rerank, KG, dedup.
Its write-side is weak (no auto-hooks, no structured extraction).

By borrowing each project's strong side, we get:
- claude-mem's write path → high-density structured observations
- MemPalace's read path → semantic search + KG + fuzzy recall
- Best of both worlds

## Storage layout

```
~/.polymem/
├── polymem.db                # SQLite (FTS5 + observations + sessions + raw backup)
└── chroma/                   # ChromaDB persistent client
    ├── chroma.sqlite3        # Metadata
    └── <uuid>/               # HNSW index per collection
```

SQLite is primary truth — ChromaDB is derived (rebuildable from SQLite).

## Known gaps / TODO

- [ ] Wire `/v1/search` to also query ChromaDB (currently FTS5-only stub)
- [ ] Wire `/v1/kg/query` endpoint in server.py (knowledge_graph.py is copied but not exposed yet)
- [ ] Wire `/v1/raw/session/:id` for full-text recall
- [ ] ChromaDB upsert on observation insert (currently SQLite-only)
- [ ] Cursor, Cline, ChatGPT collectors — stubs only, need real implementations
- [ ] `statusline-counts` equivalent for per-client memory stats
- [ ] WAL audit log (MemPalace-style) for write operations
- [ ] Client fingerprint inference (when `client` not explicitly passed)

## Design trade-offs

### Chose claude-mem's SQLite/FTS5 over MemPalace's pure ChromaDB

Why: Observations have high-value structured fields (title, facts, concepts). FTS5 on those fields
is more precise than vector search. ChromaDB handles the long-tail semantic search.

### Chose MemPalace's ChromaDB over claude-mem's optional ChromaDB

Why: MemPalace's backend has better migration/recovery (v0.6→1.5 BLOB fix, inode-based cache
invalidation). Same library, more robust wrapper.

### Chose one HTTP engine over one MCP server for writes

Why: MCP requires the model to actively call tools. For automatic collection, we need the *client*
to push events regardless of model behavior. HTTP is the universal client-to-server protocol.
MCP is retained for reads (where model-initiated queries make sense).

### Kept the claude-mem extraction prompt verbatim

Why: It's the product of significant iteration. 6 types × 7 concepts × specific XML format has
proven extraction quality. Reinventing this would regress.
