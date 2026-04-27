"""ChromaDB vector indexing for observations.

Wires the MemPalace-copied ChromaBackend into PolyMem's observation write path.
Every structured observation written to SQLite also gets indexed as a vector
document so the /v1/search endpoint can do semantic similarity search.

Design:
  - One collection: `polymem_observations`
  - Document = title + subtitle + narrative + facts joined (the semantic payload)
  - ID = `obs_{sqlite_id}` (lets us join back to the full record)
  - Metadata = {sqlite_id, client, project, type, memory_session_id, created_at_epoch}
  - Idempotent: upsert lets us safely re-index without dup
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .backends.chroma import ChromaBackend

logger = logging.getLogger("polymem.vector")

COLLECTION_NAME = "polymem_observations"


def build_document(obs: dict[str, Any]) -> str:
    """Compose the semantic payload from structured fields."""
    parts: list[str] = []
    if obs.get("title"):
        parts.append(obs["title"])
    if obs.get("subtitle"):
        parts.append(obs["subtitle"])
    if obs.get("narrative"):
        parts.append(obs["narrative"])

    facts = obs.get("facts") or []
    if isinstance(facts, str):
        try:
            facts = json.loads(facts)
        except json.JSONDecodeError:
            facts = []
    if facts:
        parts.append("\n".join(f"- {f}" for f in facts))

    return "\n\n".join(parts).strip() or (obs.get("title") or "(empty)")


def build_metadata(obs_id: int, obs: dict[str, Any]) -> dict[str, Any]:
    """Build ChromaDB metadata — flat scalar types only."""
    return {
        "sqlite_id": obs_id,
        "client": obs.get("client", ""),
        "model": obs.get("model") or "",
        "project": obs.get("project", ""),
        "type": obs.get("type", ""),
        "memory_session_id": obs.get("memory_session_id", ""),
        "created_at_epoch": obs.get("created_at_epoch", 0),
    }


class ObservationVectorStore:
    """Wraps the ChromaDB collection used for observation search."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir) / "chroma"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backend = ChromaBackend()
        self.col = self.backend.get_collection(
            palace_path=str(self.data_dir),
            collection_name=COLLECTION_NAME,
            create=True,
        )
        logger.info("ObservationVectorStore ready at %s", self.data_dir)

    def index(self, obs_id: int, obs: dict[str, Any]) -> None:
        """Index one observation. Safe to call repeatedly (upsert)."""
        try:
            doc = build_document(obs)
            meta = build_metadata(obs_id, obs)
            self.col.upsert(
                documents=[doc],
                ids=[f"obs_{obs_id}"],
                metadatas=[meta],
            )
        except Exception as e:
            logger.exception("ChromaDB index failed for obs_id=%s: %s", obs_id, e)

    def search(
        self,
        query: str,
        n_results: int = 10,
        project: Optional[str] = None,
        client: Optional[str] = None,
        type_filter: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Semantic search. Returns hits with sqlite_id, distance, document preview."""
        where: dict[str, Any] = {}
        if project:
            where["project"] = project
        if client:
            where["client"] = client
        if type_filter:
            where["type"] = type_filter
        # ChromaDB requires $and for multi-field where clauses
        chroma_where = None
        if where:
            if len(where) == 1:
                chroma_where = where
            else:
                chroma_where = {"$and": [{k: v} for k, v in where.items()]}

        try:
            result = self.col.query(
                query_texts=[query],
                n_results=n_results,
                where=chroma_where,
            )
        except Exception as e:
            logger.exception("ChromaDB query failed: %s", e)
            return []

        hits: list[dict[str, Any]] = []
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]

        for i, vector_id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            doc = docs[i] if i < len(docs) else ""
            dist = dists[i] if i < len(dists) else None
            hits.append({
                "sqlite_id": meta.get("sqlite_id"),
                "vector_id": vector_id,
                "distance": dist,
                "similarity": (1.0 - dist) if dist is not None else None,
                "client": meta.get("client"),
                "type": meta.get("type"),
                "project": meta.get("project"),
                "preview": doc[:300],
            })
        return hits

    def count(self) -> int:
        try:
            return self.col.count()
        except Exception:
            return -1

    def delete(self, obs_id: int) -> None:
        try:
            self.col.delete(ids=[f"obs_{obs_id}"])
        except Exception as e:
            logger.exception("ChromaDB delete failed for obs_id=%s: %s", obs_id, e)
