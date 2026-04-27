# PolyMem Engine HTTP API

Base URL: `http://127.0.0.1:37700`

## Write endpoints

### `POST /v1/sessions/init`

```json
{
  "client": "claude_code",
  "client_session_id": "abc-123",
  "project": "cses-client",
  "model": "claude-opus-4-7",
  "user_prompt": "Fix the race condition"
}
```
→ `{"memory_session_id": "uuid"}`

### `POST /v1/sessions/complete`

```json
{ "memory_session_id": "uuid", "status": "completed" }
```

### `POST /v1/observations/pending`

**Primary write endpoint.** Enqueues for async LLM extraction.

```json
{
  "memory_session_id": "uuid",
  "client": "claude_code",
  "tool_name": "Edit",
  "tool_input": "...",
  "tool_response": "...",
  "cwd": "/Users/me/project"
}
```
→ `{"pending_id": 42}`

### `POST /v1/observations`

Direct write (for collectors that do their own extraction, or test fixtures).

```json
{
  "memory_session_id": "uuid",
  "client": "claude_code",
  "project": "my-project",
  "type": "bugfix",
  "title": "...",
  "narrative": "...",
  "facts": ["..."],
  "concepts": ["problem-solution"]
}
```
→ `{"id": 123, "deduped": false}`

### `POST /v1/summaries/pending`

Enqueue summary extraction at session stop.

### `POST /v1/raw`

Full-text backup (unstructured). Any role/content stored verbatim.

## Read endpoints

### `GET /v1/search?query=...&project=...&client=...&type=...&limit=20`

FTS5 full-text search with snippet highlighting.

### `GET /v1/observations/{id}`

Full observation record with parsed JSON fields.

### `GET /v1/context?project=...&client=...&max_obs=50`

Returns `$PMEM` context block — ready to inject at session start.

### `GET /v1/health`

Liveness check.

## Configuration

Set via environment variables before starting the engine:

| Variable | Default | Description |
|----------|---------|-------------|
| `POLYMEM_PORT` | 37700 | HTTP port |
| `POLYMEM_HOST` | 127.0.0.1 | Bind host |
| `POLYMEM_DATA_DIR` | `~/.polymem` | Storage directory |
| `POLYMEM_PROVIDER` | `openrouter` | LLM provider: anthropic / openai / gemini / ollama / openrouter |
| `POLYMEM_MODEL` | `xiaomi/mimo-v2-flash:free` | Model ID for extraction |
| `POLYMEM_ENDPOINT` | (provider-inferred) | Override endpoint URL |
| `POLYMEM_API_KEY` | (empty) | API key if provider requires auth |
