"""SQLite schema with FTS5 — adapted from claude-mem v10.6.2.

Additions over claude-mem:
  - `client` column (claude_code / cursor / cline / chatgpt / aider / ...)
  - `model` column (claude-sonnet-4-6 / gpt-4o / gemini-2.0-pro / ...)
  - `raw_conversation` table for full-text backup (MemPalace-style)

Kept from claude-mem:
  - Hierarchical observation fields (title/subtitle/narrative/facts/concepts)
  - FTS5 virtual tables with auto-maintained triggers
  - content_hash dedup (sha256[:16])
  - pending_messages async queue
  - Session/summary structure
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


DEFAULT_DB_PATH = Path.home() / ".polymem" / "polymem.db"


# ─── Schema ────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_session_id TEXT UNIQUE NOT NULL,
    memory_session_id TEXT UNIQUE NOT NULL,
    client TEXT NOT NULL,
    model TEXT,
    project TEXT NOT NULL,
    user_prompt TEXT,
    custom_title TEXT,
    started_at TEXT NOT NULL,
    started_at_epoch INTEGER NOT NULL,
    completed_at TEXT,
    completed_at_epoch INTEGER,
    status TEXT CHECK(status IN ('active', 'completed', 'failed')) NOT NULL DEFAULT 'active',
    prompt_counter INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_client ON sessions(client);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at_epoch DESC);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_session_id TEXT NOT NULL,
    client TEXT NOT NULL,
    model TEXT,
    project TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT,
    subtitle TEXT,
    narrative TEXT,
    facts TEXT,
    concepts TEXT,
    files_read TEXT,
    files_modified TEXT,
    prompt_number INTEGER,
    discovery_tokens INTEGER DEFAULT 0,
    content_hash TEXT,
    created_at TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    FOREIGN KEY(memory_session_id) REFERENCES sessions(memory_session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(memory_session_id);
CREATE INDEX IF NOT EXISTS idx_obs_client ON observations(client);
CREATE INDEX IF NOT EXISTS idx_obs_project ON observations(project);
CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(type);
CREATE INDEX IF NOT EXISTS idx_obs_created ON observations(created_at_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_obs_hash ON observations(content_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    title, subtitle, narrative, facts, concepts,
    content='observations', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS obs_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observations_fts(rowid, title, subtitle, narrative, facts, concepts)
    VALUES (new.id, new.title, new.subtitle, new.narrative, new.facts, new.concepts);
END;

CREATE TRIGGER IF NOT EXISTS obs_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, title, subtitle, narrative, facts, concepts)
    VALUES('delete', old.id, old.title, old.subtitle, old.narrative, old.facts, old.concepts);
END;

CREATE TRIGGER IF NOT EXISTS obs_au AFTER UPDATE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, title, subtitle, narrative, facts, concepts)
    VALUES('delete', old.id, old.title, old.subtitle, old.narrative, old.facts, old.concepts);
    INSERT INTO observations_fts(rowid, title, subtitle, narrative, facts, concepts)
    VALUES (new.id, new.title, new.subtitle, new.narrative, new.facts, new.concepts);
END;

CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_session_id TEXT NOT NULL,
    client TEXT NOT NULL,
    project TEXT NOT NULL,
    request TEXT,
    investigated TEXT,
    learned TEXT,
    completed TEXT,
    next_steps TEXT,
    notes TEXT,
    prompt_number INTEGER,
    discovery_tokens INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    FOREIGN KEY(memory_session_id) REFERENCES sessions(memory_session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sum_session ON session_summaries(memory_session_id);
CREATE INDEX IF NOT EXISTS idx_sum_project ON session_summaries(project);
CREATE INDEX IF NOT EXISTS idx_sum_created ON session_summaries(created_at_epoch DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS session_summaries_fts USING fts5(
    request, investigated, learned, completed, next_steps, notes,
    content='session_summaries', content_rowid='id'
);

-- Full-text conversation backup (MemPalace-style, unstructured)
CREATE TABLE IF NOT EXISTS raw_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_session_id TEXT NOT NULL,
    client TEXT NOT NULL,
    model TEXT,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'tool', 'system')),
    content TEXT NOT NULL,
    tool_name TEXT,
    tool_input TEXT,
    tool_response TEXT,
    prompt_number INTEGER,
    created_at_epoch INTEGER NOT NULL,
    FOREIGN KEY(memory_session_id) REFERENCES sessions(memory_session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_raw_session ON raw_conversations(memory_session_id);
CREATE INDEX IF NOT EXISTS idx_raw_created ON raw_conversations(created_at_epoch DESC);

-- Pending queue for async LLM extraction
CREATE TABLE IF NOT EXISTS pending_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_session_id TEXT NOT NULL,
    client TEXT NOT NULL,
    model TEXT,
    message_type TEXT NOT NULL CHECK(message_type IN ('observation', 'summarize')),
    tool_name TEXT,
    tool_input TEXT,
    tool_response TEXT,
    cwd TEXT,
    last_user_message TEXT,
    last_assistant_message TEXT,
    prompt_number INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'processing', 'processed', 'failed')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at_epoch INTEGER NOT NULL,
    started_at_epoch INTEGER,
    completed_at_epoch INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_messages(status, created_at_epoch);
"""


DEDUP_WINDOW_MS = 30_000  # 30 seconds, same as claude-mem


def content_hash(memory_session_id: str, title: str, narrative: str) -> str:
    """sha256[:16] — deterministic dedup key (claude-mem pattern)."""
    h = hashlib.sha256()
    h.update((memory_session_id or "").encode())
    h.update((title or "").encode())
    h.update((narrative or "").encode())
    return h.hexdigest()[:16]


class Database:
    """Thin wrapper over sqlite3 with schema bootstrap."""

    def __init__(self, db_path: Optional[Path] = None):
        self.path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def now(self) -> tuple[str, int]:
        epoch_ms = int(time.time() * 1000)
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch_ms / 1000))
        return iso, epoch_ms

    # ─── Sessions ──────────────────────────────────────────────────────

    def ensure_session(
        self,
        client_session_id: str,
        memory_session_id: str,
        client: str,
        project: str,
        model: Optional[str] = None,
        user_prompt: Optional[str] = None,
    ) -> None:
        iso, epoch = self.now()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO sessions
            (client_session_id, memory_session_id, client, model, project,
             user_prompt, started_at, started_at_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (client_session_id, memory_session_id, client, model, project,
             user_prompt, iso, epoch),
        )
        self.conn.commit()

    def complete_session(self, memory_session_id: str, status: str = "completed") -> None:
        iso, epoch = self.now()
        self.conn.execute(
            """
            UPDATE sessions
            SET status = ?, completed_at = ?, completed_at_epoch = ?
            WHERE memory_session_id = ?
            """,
            (status, iso, epoch, memory_session_id),
        )
        self.conn.commit()

    # ─── Observations ──────────────────────────────────────────────────

    def insert_observation(self, obs: dict[str, Any]) -> Optional[int]:
        """Insert observation with content_hash dedup (30s window)."""
        h = content_hash(obs["memory_session_id"], obs.get("title", ""), obs.get("narrative", ""))
        iso, epoch = self.now()

        # Dedup check
        cutoff = epoch - DEDUP_WINDOW_MS
        existing = self.conn.execute(
            "SELECT id FROM observations WHERE content_hash = ? AND created_at_epoch > ?",
            (h, cutoff),
        ).fetchone()
        if existing:
            return None

        cur = self.conn.execute(
            """
            INSERT INTO observations
            (memory_session_id, client, model, project, type,
             title, subtitle, narrative, facts, concepts,
             files_read, files_modified, prompt_number, discovery_tokens,
             content_hash, created_at, created_at_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs["memory_session_id"],
                obs["client"],
                obs.get("model"),
                obs["project"],
                obs["type"],
                obs.get("title"),
                obs.get("subtitle"),
                obs.get("narrative"),
                json.dumps(obs.get("facts") or []),
                json.dumps(obs.get("concepts") or []),
                json.dumps(obs.get("files_read") or []),
                json.dumps(obs.get("files_modified") or []),
                obs.get("prompt_number"),
                obs.get("discovery_tokens", 0),
                h,
                iso,
                epoch,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def insert_summary(self, summary: dict[str, Any]) -> int:
        iso, epoch = self.now()
        cur = self.conn.execute(
            """
            INSERT INTO session_summaries
            (memory_session_id, client, project, request, investigated,
             learned, completed, next_steps, notes, prompt_number,
             discovery_tokens, created_at, created_at_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary["memory_session_id"],
                summary["client"],
                summary["project"],
                summary.get("request"),
                summary.get("investigated"),
                summary.get("learned"),
                summary.get("completed"),
                summary.get("next_steps"),
                summary.get("notes"),
                summary.get("prompt_number"),
                summary.get("discovery_tokens", 0),
                iso,
                epoch,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    # ─── Raw conversation backup ───────────────────────────────────────

    def insert_raw(
        self,
        memory_session_id: str,
        client: str,
        role: str,
        content: str,
        model: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_input: Optional[str] = None,
        tool_response: Optional[str] = None,
        prompt_number: Optional[int] = None,
    ) -> int:
        _, epoch = self.now()
        cur = self.conn.execute(
            """
            INSERT INTO raw_conversations
            (memory_session_id, client, model, role, content,
             tool_name, tool_input, tool_response, prompt_number, created_at_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (memory_session_id, client, model, role, content,
             tool_name, tool_input, tool_response, prompt_number, epoch),
        )
        self.conn.commit()
        return cur.lastrowid

    # ─── Pending queue ─────────────────────────────────────────────────

    def enqueue_pending(self, msg: dict[str, Any]) -> int:
        _, epoch = self.now()
        cur = self.conn.execute(
            """
            INSERT INTO pending_messages
            (memory_session_id, client, model, message_type, tool_name,
             tool_input, tool_response, cwd, last_user_message,
             last_assistant_message, prompt_number, created_at_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg["memory_session_id"],
                msg["client"],
                msg.get("model"),
                msg["message_type"],
                msg.get("tool_name"),
                msg.get("tool_input"),
                msg.get("tool_response"),
                msg.get("cwd"),
                msg.get("last_user_message"),
                msg.get("last_assistant_message"),
                msg.get("prompt_number"),
                epoch,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def claim_pending(self, batch_size: int = 5) -> list[sqlite3.Row]:
        """Atomically claim pending rows for processing."""
        _, epoch = self.now()
        rows = self.conn.execute(
            """
            SELECT * FROM pending_messages
            WHERE status = 'pending' AND retry_count < 3
            ORDER BY created_at_epoch ASC
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        if not rows:
            return []
        ids = tuple(r["id"] for r in rows)
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(
            f"UPDATE pending_messages SET status = 'processing', started_at_epoch = ? WHERE id IN ({placeholders})",
            (epoch, *ids),
        )
        self.conn.commit()
        return rows

    def mark_processed(self, pending_id: int, ok: bool = True) -> None:
        _, epoch = self.now()
        self.conn.execute(
            """
            UPDATE pending_messages
            SET status = ?, completed_at_epoch = ?, retry_count = retry_count + ?
            WHERE id = ?
            """,
            ("processed" if ok else "failed", epoch, 0 if ok else 1, pending_id),
        )
        self.conn.commit()
