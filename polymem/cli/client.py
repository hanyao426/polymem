"""HTTP client to the PolyMem engine.

Stdlib-only (no `requests`) so the hook entry point — which spawns a fresh
subprocess on every Claude Code tool call — has minimal cold-start cost.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE = os.environ.get("POLYMEM_BASE_URL", "http://127.0.0.1:37700")


class PolyMemClient:
    def __init__(self, base: str = DEFAULT_BASE, timeout: float = 10.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    # ─── transport ───────────────────────────────────────────────────────

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(self.base + path, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ─── public API ──────────────────────────────────────────────────────

    def is_healthy(self) -> bool:
        try:
            return self._get("/v1/health").get("status") == "ok"
        except Exception:
            return False

    def session_init(
        self,
        *,
        client: str,
        client_session_id: str,
        project: str,
        model: str | None = None,
        user_prompt: str | None = None,
    ) -> str:
        payload = {
            "client": client,
            "client_session_id": client_session_id,
            "project": project,
        }
        if model:
            payload["model"] = model
        if user_prompt:
            payload["user_prompt"] = user_prompt
        return self._post("/v1/sessions/init", payload)["memory_session_id"]

    def session_complete(self, memory_session_id: str, status: str = "completed") -> None:
        self._post("/v1/sessions/complete", {
            "memory_session_id": memory_session_id,
            "status": status,
        })

    def pending_observation(
        self,
        *,
        memory_session_id: str,
        client: str,
        tool_name: str,
        tool_input: str = "",
        tool_response: str = "",
        cwd: str | None = None,
        model: str | None = None,
    ) -> int:
        payload = {
            "memory_session_id": memory_session_id,
            "client": client,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_response": tool_response,
        }
        if cwd:
            payload["cwd"] = cwd
        if model:
            payload["model"] = model
        return self._post("/v1/observations/pending", payload)["pending_id"]

    def pending_summary(
        self,
        *,
        memory_session_id: str,
        client: str,
        last_user_message: str,
        last_assistant_message: str,
    ) -> int:
        return self._post("/v1/summaries/pending", {
            "memory_session_id": memory_session_id,
            "client": client,
            "last_user_message": last_user_message,
            "last_assistant_message": last_assistant_message,
        })["pending_id"]

    def get_context(
        self,
        *,
        project: str,
        client: str | None = None,
        lite: bool = False,
        days: int | None = None,
        max_obs: int | None = None,
        show_summary: bool = True,
    ) -> str:
        from urllib.parse import urlencode
        q: dict[str, Any] = {"project": project}
        if client:
            q["client"] = client
        if lite:
            q["lite"] = "true"
        if days is not None:
            q["days"] = days
        if max_obs is not None:
            q["max_obs"] = max_obs
        if not show_summary:
            q["show_summary"] = "false"
        return self._get("/v1/context?" + urlencode(q))["context"]

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        from urllib.parse import urlencode
        q = {"query": query, **kwargs}
        return self._get("/v1/search?" + urlencode(q))

    def get_observations(self, ids: list[int]) -> list[dict[str, Any]]:
        return [self._get(f"/v1/observations/{i}") for i in ids]

    def kg_query(
        self,
        entity: str,
        as_of: str | None = None,
        direction: str = "both",
    ) -> dict[str, Any]:
        from urllib.parse import urlencode
        q: dict[str, Any] = {"entity": entity, "direction": direction}
        if as_of:
            q["as_of"] = as_of
        return self._get("/v1/kg/query?" + urlencode(q))

    def raw_session(self, memory_session_id: str, limit: int = 100) -> dict[str, Any]:
        from urllib.parse import urlencode
        return self._get(
            f"/v1/raw/session/{memory_session_id}?" + urlencode({"limit": limit})
        )

    def report(
        self,
        date: str | None = None,
        project: str | None = None,
        client: str | None = None,
        format: str = "markdown",
    ) -> dict[str, Any]:
        from urllib.parse import urlencode
        q: dict[str, Any] = {"format": format}
        if date:
            q["date"] = date
        if project:
            q["project"] = project
        if client:
            q["client"] = client
        return self._get("/v1/report?" + urlencode(q))
