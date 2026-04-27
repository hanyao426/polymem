"""PolyMem — Cross-model automated memory system.

Borrows from:
  - MemPalace (ChromaDB backend, searcher, knowledge_graph, dedup, palace)
  - claude-mem (SQLite schema + FTS5, observation model, LLM extraction prompts,
                context injection format, 3-layer search workflow)

Architecture:
  Collectors (per-client) → Engine HTTP API → Storage → MCP Server (for reads)

See ARCHITECTURE.md for full design.
"""

__version__ = "0.1.0"
