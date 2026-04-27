"""PolyMem HTTP server — the unified write + read endpoint for all collectors.

Endpoints (mirrors claude-mem's worker API, adds client field):
  POST   /v1/sessions/init
  POST   /v1/sessions/complete
  POST   /v1/observations            # direct write (pre-extracted)
  POST   /v1/observations/pending    # enqueue for async LLM extraction
  POST   /v1/summaries               # direct summary write
  POST   /v1/summaries/pending       # enqueue summary extraction
  POST   /v1/raw                     # full-text backup
  GET    /v1/context                 # $PMEM context block
  GET    /v1/search                  # FTS5 + vector hybrid
  GET    /v1/observations/:id
  POST   /v1/observations/batch
  GET    /v1/health

Runs on localhost:37700 by default (different from claude-mem's 37777 to avoid conflict).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException, Query
    from pydantic import BaseModel
    import uvicorn
except ImportError:
    raise SystemExit(
        "PolyMem requires: pip install fastapi uvicorn chromadb\n"
        "Run: cd ~/demo/polymem && pip install -r requirements.txt"
    )

from .db import Database
from .context_generator import ContextOptions, generate_context
from .extractor import LLMConfig, extract_observation, extract_summary
from .vector_store import ObservationVectorStore
from .knowledge_graph import KnowledgeGraph
from .report import ReportOptions, generate_report, generate_report_json

logger = logging.getLogger("polymem")

POLYMEM_PORT = int(os.getenv("POLYMEM_PORT", "37700"))
POLYMEM_HOST = os.getenv("POLYMEM_HOST", "127.0.0.1")
DATA_DIR = Path(os.getenv("POLYMEM_DATA_DIR", Path.home() / ".polymem"))


# ─── Request models ────────────────────────────────────────────────────────

class SessionInit(BaseModel):
    client: str
    client_session_id: str
    project: str
    model: str | None = None
    user_prompt: str | None = None


class SessionComplete(BaseModel):
    memory_session_id: str
    status: str = "completed"


class PendingObservation(BaseModel):
    memory_session_id: str
    client: str
    model: str | None = None
    tool_name: str
    tool_input: str = ""
    tool_response: str = ""
    cwd: str | None = None
    prompt_number: int | None = None


class DirectObservation(BaseModel):
    memory_session_id: str
    client: str
    model: str | None = None
    project: str
    type: str
    title: str
    subtitle: str | None = None
    narrative: str | None = None
    facts: list[str] = []
    concepts: list[str] = []
    files_read: list[str] = []
    files_modified: list[str] = []
    prompt_number: int | None = None


class PendingSummary(BaseModel):
    memory_session_id: str
    client: str
    last_user_message: str
    last_assistant_message: str
    prompt_number: int | None = None


class RawMessage(BaseModel):
    memory_session_id: str
    client: str
    model: str | None = None
    role: str
    content: str
    tool_name: str | None = None
    tool_input: str | None = None
    tool_response: str | None = None
    prompt_number: int | None = None


class KGTriple(BaseModel):
    subject: str
    predicate: str
    object: str
    valid_from: str | None = None
    confidence: float = 1.0
    source: str | None = None


class KGInvalidate(BaseModel):
    subject: str
    predicate: str
    object: str
    ended: str | None = None


# ─── Worker (async LLM extraction) ─────────────────────────────────────────


class ExtractionWorker(threading.Thread):
    """Background thread: polls pending_messages, calls LLM, writes observations."""

    def __init__(self, db: Database, cfg: LLMConfig, vector: ObservationVectorStore, poll_interval: float = 2.0):
        super().__init__(daemon=True)
        self.db = db
        self.cfg = cfg
        self.vector = vector
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        logger.info("ExtractionWorker started (provider=%s model=%s)", self.cfg.provider, self.cfg.model)
        while not self._stop.is_set():
            try:
                rows = self.db.claim_pending(batch_size=3)
                if not rows:
                    time.sleep(self.poll_interval)
                    continue
                for row in rows:
                    self._process(row)
            except Exception as e:
                logger.exception("Worker error: %s", e)
                time.sleep(self.poll_interval)

    def _process(self, row) -> None:
        try:
            # Look up project for this session
            s = self.db.conn.execute(
                "SELECT project FROM sessions WHERE memory_session_id = ?",
                (row["memory_session_id"],),
            ).fetchone()
            project = s["project"] if s else "unknown"

            if row["message_type"] == "observation":
                obs, tokens = extract_observation(
                    tool_name=row["tool_name"] or "",
                    tool_input=row["tool_input"] or "",
                    tool_response=row["tool_response"] or "",
                    cwd=row["cwd"],
                    cfg=self.cfg,
                )
                if obs:
                    obs["memory_session_id"] = row["memory_session_id"]
                    obs["client"] = row["client"]
                    obs["model"] = row["model"]
                    obs["project"] = project
                    obs["prompt_number"] = row["prompt_number"]
                    obs["discovery_tokens"] = tokens
                    obs_id = self.db.insert_observation(obs)
                    if obs_id:  # not deduped
                        _, epoch = self.db.now()
                        obs["created_at_epoch"] = epoch
                        self.vector.index(obs_id, obs)
            elif row["message_type"] == "summarize":
                summary, tokens = extract_summary(
                    last_user_message=row["last_user_message"] or "",
                    last_assistant_message=row["last_assistant_message"] or "",
                    cfg=self.cfg,
                )
                if summary:
                    summary["memory_session_id"] = row["memory_session_id"]
                    summary["client"] = row["client"]
                    summary["project"] = project
                    summary["prompt_number"] = row["prompt_number"]
                    summary["discovery_tokens"] = tokens
                    self.db.insert_summary(summary)
            self.db.mark_processed(row["id"], ok=True)
        except Exception as e:
            logger.exception("Failed to process pending id=%s: %s", row["id"], e)
            self.db.mark_processed(row["id"], ok=False)


# ─── App factory ───────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = Database(DATA_DIR / "polymem.db")
    cfg = LLMConfig()
    vector = ObservationVectorStore(DATA_DIR)
    kg = KnowledgeGraph(str(DATA_DIR / "kg.db"))
    worker = ExtractionWorker(db, cfg, vector)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        yield
        worker.stop()

    app = FastAPI(title="PolyMem Engine", version="0.1.0", lifespan=lifespan)

    @app.get("/v1/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/v1/sessions/init")
    def session_init(s: SessionInit):
        # Idempotent: if already initialized, return existing memory_session_id.
        # Hooks fire stateless (one subprocess per event) so this is called
        # multiple times per session — must not mint new IDs each time.
        existing = db.conn.execute(
            "SELECT memory_session_id FROM sessions WHERE client_session_id = ?",
            (s.client_session_id,),
        ).fetchone()
        if existing:
            return {"memory_session_id": existing["memory_session_id"]}

        memory_session_id = str(uuid.uuid4())
        db.ensure_session(
            client_session_id=s.client_session_id,
            memory_session_id=memory_session_id,
            client=s.client,
            project=s.project,
            model=s.model,
            user_prompt=s.user_prompt,
        )
        return {"memory_session_id": memory_session_id}

    @app.post("/v1/sessions/complete")
    def session_complete(s: SessionComplete):
        db.complete_session(s.memory_session_id, s.status)
        return {"ok": True}

    @app.post("/v1/observations")
    def direct_observation(o: DirectObservation):
        obs_dict = o.model_dump()
        obs_id = db.insert_observation(obs_dict)
        if obs_id:
            _, epoch = db.now()
            obs_dict["created_at_epoch"] = epoch
            vector.index(obs_id, obs_dict)
        return {"id": obs_id, "deduped": obs_id is None}

    @app.post("/v1/observations/pending")
    def pending_observation(o: PendingObservation):
        data = o.dict()
        data["message_type"] = "observation"
        pid = db.enqueue_pending(data)
        return {"pending_id": pid}

    @app.post("/v1/summaries/pending")
    def pending_summary(s: PendingSummary):
        data = s.dict()
        data["message_type"] = "summarize"
        pid = db.enqueue_pending(data)
        return {"pending_id": pid}

    @app.post("/v1/raw")
    def raw(m: RawMessage):
        rid = db.insert_raw(**m.dict())
        return {"id": rid}

    @app.get("/v1/context")
    def context(
        project: str,
        client: str | None = None,
        max_obs: int = 50,
        lite: bool = False,
        days: int | None = None,
        show_summary: bool = True,
    ):
        opts = ContextOptions(
            project=project,
            client=client,
            max_observations=max_obs,
            lite=lite,
            days=days,
            show_last_summary=show_summary,
        )
        return {"context": generate_context(db, opts)}

    def _to_fts5_query(raw: str) -> str:
        """
        Make any user input safe for FTS5 MATCH.

        FTS5 treats `- : * ^ "` and parens as operators. A query like
        "2026-04-27 今天" parses as `2026 NOT 04:27 ...` and raises
        OperationalError. Strategy: split on whitespace, wrap each
        non-empty token in double quotes (FTS5 phrase literal),
        escape any embedded double quotes by doubling them.
        """
        tokens = [t for t in raw.split() if t.strip()]
        if not tokens:
            return '""'
        quoted = [f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens]
        return " ".join(quoted)

    @app.get("/v1/search")
    def search(
        q: str = Query(..., alias="query"),
        project: str | None = None,
        client: str | None = None,
        type: str | None = None,
        limit: int = 20,
        mode: str = "hybrid",  # hybrid | fts | semantic
    ):
        """
        Search modes:
          - fts      : FTS5 keyword (fast, exact)
          - semantic : ChromaDB vector similarity
          - hybrid   : both, merged by rank (default)
        """
        filters = []
        params: list = []
        if project:
            filters.append("o.project = ?")
            params.append(project)
        if client:
            filters.append("o.client = ?")
            params.append(client)
        if type:
            filters.append("o.type = ?")
            params.append(type)
        where = " AND ".join(filters)
        where_clause = f"AND {where}" if where else ""

        fts_query = _to_fts5_query(q)

        fts_results: list[dict] = []
        if mode in ("fts", "hybrid"):
            try:
                rows = db.conn.execute(
                    f"""
                    SELECT o.id, o.client, o.type, o.title, o.subtitle, o.created_at_epoch,
                           snippet(observations_fts, -1, '<b>', '</b>', '...', 16) AS snippet,
                           rank AS fts_rank
                    FROM observations_fts fts
                    JOIN observations o ON o.id = fts.rowid
                    WHERE observations_fts MATCH ? {where_clause}
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, *params, limit * 2 if mode == "hybrid" else limit),
                ).fetchall()
                fts_results = [{**dict(r), "source": "fts"} for r in rows]
            except sqlite3.OperationalError as e:
                # Defensive fallback — never let FTS5 errors blow up the response.
                # Semantic side still works.
                logger.warning("FTS5 query failed for %r → %r: %s", q, fts_query, e)
                fts_results = []

        semantic_results: list[dict] = []
        if mode in ("semantic", "hybrid"):
            hits = vector.search(
                query=q,
                n_results=limit * 2 if mode == "hybrid" else limit,
                project=project,
                client=client,
                type_filter=type,
            )
            semantic_results = [{**h, "source": "semantic"} for h in hits]

        if mode == "fts":
            return {"mode": "fts", "results": fts_results[:limit]}
        if mode == "semantic":
            return {"mode": "semantic", "results": semantic_results[:limit]}

        # hybrid: merge by sqlite_id, score = inverse_fts_rank + semantic_similarity
        by_id: dict[int, dict] = {}
        for i, r in enumerate(fts_results):
            sid = r.get("id")
            if sid is None:
                continue
            score = 1.0 / (i + 1)  # reciprocal rank
            by_id[sid] = {**r, "fts_score": score, "semantic_score": 0.0}
        for h in semantic_results:
            sid = h.get("sqlite_id")
            if sid is None:
                continue
            sem_score = h.get("similarity") or 0.0
            if sid in by_id:
                by_id[sid]["semantic_score"] = sem_score
                by_id[sid]["source"] = "both"
            else:
                by_id[sid] = {
                    "id": sid,
                    "client": h.get("client"),
                    "type": h.get("type"),
                    "preview": h.get("preview"),
                    "fts_score": 0.0,
                    "semantic_score": sem_score,
                    "source": "semantic",
                }
        for r in by_id.values():
            r["hybrid_score"] = 0.4 * r.get("fts_score", 0) + 0.6 * r.get("semantic_score", 0)
        merged = sorted(by_id.values(), key=lambda x: x["hybrid_score"], reverse=True)[:limit]
        return {"mode": "hybrid", "results": merged}

    @app.post("/v1/reindex")
    def reindex():
        """Rebuild ChromaDB index from SQLite (for recovery or first-time population)."""
        rows = db.conn.execute(
            "SELECT id, memory_session_id, client, model, project, type, "
            "title, subtitle, narrative, facts, created_at_epoch "
            "FROM observations"
        ).fetchall()
        count = 0
        for r in rows:
            d = dict(r)
            try:
                d["facts"] = json.loads(d["facts"] or "[]")
            except Exception:
                d["facts"] = []
            vector.index(r["id"], d)
            count += 1
        return {"indexed": count, "vector_count": vector.count()}

    @app.get("/v1/vector/stats")
    def vector_stats():
        return {"count": vector.count()}

    @app.get("/v1/observations/{obs_id}")
    def get_observation(obs_id: int):
        row = db.conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
        if not row:
            raise HTTPException(404, "observation not found")
        d = dict(row)
        for k in ("facts", "concepts", "files_read", "files_modified"):
            d[k] = json.loads(d[k] or "[]")
        return d

    # ─── Knowledge graph endpoints (borrowed from MemPalace) ─────────────

    @app.post("/v1/kg/add")
    def kg_add(t: KGTriple):
        try:
            triple_id = kg.add_triple(
                subject=t.subject,
                predicate=t.predicate,
                obj=t.object,
                valid_from=t.valid_from,
                confidence=t.confidence,
                source_file=t.source,
            )
            return {"triple_id": triple_id}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/v1/kg/invalidate")
    def kg_invalidate(t: KGInvalidate):
        try:
            count = kg.invalidate(t.subject, t.predicate, t.object, ended=t.ended)
            return {"invalidated": count}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/v1/kg/query")
    def kg_query(
        entity: str,
        as_of: str | None = None,
        direction: str = "both",
    ):
        try:
            results = kg.query_entity(entity, as_of=as_of, direction=direction)
            return {"entity": entity, "triples": results}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/v1/kg/timeline")
    def kg_timeline(entity: str | None = None):
        try:
            return {"timeline": kg.timeline(entity_name=entity)}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/v1/kg/stats")
    def kg_stats():
        with kg._lock:
            conn = kg._conn()
            entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            triple_count = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
            active_count = conn.execute(
                "SELECT COUNT(*) FROM triples WHERE valid_to IS NULL"
            ).fetchone()[0]
            predicate_rows = conn.execute(
                "SELECT predicate, COUNT(*) AS n FROM triples GROUP BY predicate ORDER BY n DESC LIMIT 20"
            ).fetchall()
        return {
            "entities": entity_count,
            "triples": triple_count,
            "active_triples": active_count,
            "top_predicates": [{"predicate": r[0], "count": r[1]} for r in predicate_rows],
        }

    # ─── Daily report ───────────────────────────────────────────────────

    @app.get("/v1/report")
    def daily_report(
        date: str | None = None,
        project: str | None = None,
        client: str | None = None,
        format: str = "markdown",
        group_by_theme: bool = True,
    ):
        """
        Generate a daily report of today's (or specified date's) observations.

        format=markdown → returns {"report": "<markdown>"}
        format=json     → returns structured data
        """
        opts = ReportOptions(
            date=date,
            project=project,
            client=client,
            group_by_theme=group_by_theme,
        )
        if format == "json":
            return generate_report_json(db, opts)
        return {"report": generate_report(db, opts)}

    # ─── Raw conversation recall ────────────────────────────────────────

    @app.get("/v1/raw/session/{session_id}")
    def raw_session(session_id: str, limit: int = 200):
        rows = db.conn.execute(
            """
            SELECT id, client, model, role, content, tool_name,
                   prompt_number, created_at_epoch
            FROM raw_conversations
            WHERE memory_session_id = ?
            ORDER BY created_at_epoch ASC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return {"messages": [dict(r) for r in rows]}

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = create_app()
    uvicorn.run(app, host=POLYMEM_HOST, port=POLYMEM_PORT, log_level="info")


if __name__ == "__main__":
    main()
