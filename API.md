# PolyMem API 文档

> 跨模型记忆系统的完整接口手册。
> 引擎：HTTP REST（FastAPI），默认监听 `http://127.0.0.1:37700`
> 读层：MCP stdio server（`polymem/cli/mcp_server.py`）
> 协议版本：v0.1.0

---

## 目录

- [快速开始](#快速开始)
- [总览](#总览)
- [HTTP API](#http-api)
  - [健康 / 元信息](#健康--元信息)
  - [会话](#会话)
  - [观察（Observations）](#观察observations)
  - [总结（Summaries）](#总结summaries)
  - [原文（Raw Conversations）](#原文raw-conversations)
  - [上下文（$PMEM 注入块）](#上下文pmem-注入块)
  - [搜索](#搜索)
  - [向量库](#向量库)
  - [知识图谱](#知识图谱)
  - [日报](#日报)
- [MCP 工具](#mcp-工具)
- [环境变量](#环境变量)
- [Hooks 模式](#hooks-模式)
- [常见错误](#常见错误)

---

## 快速开始

```bash
# 1. 启动引擎
polymem engine

# 2. 健康检查
curl http://127.0.0.1:37700/v1/health
# → {"status":"ok","version":"0.1.0"}

# 3. 装 hooks（推荐混合模式）
polymem install claude-code --mode hybrid

# 4. 拉一段轻量上下文
curl "http://127.0.0.1:37700/v1/context?project=cses-40&lite=true&days=3"
```

---

## 总览

| 维度 | 入口 |
|------|------|
| 写入端 | HTTP POST（采集器调用） |
| 读取端 | HTTP GET 或 MCP 工具（模型/CLI 调用） |
| 数据底座 | SQLite + FTS5 + ChromaDB + KG SQLite |
| LLM 提取 | 后台 `ExtractionWorker`（异步消化 `pending_messages`） |

所有接口以 `/v1/` 为前缀。请求 / 响应均为 JSON。错误格式：

```json
{ "detail": "<人类可读错误描述>" }
```

HTTP 状态码：`200` 成功 · `400` 参数错误 · `404` 未找到 · `500` 内部错误。

---

## HTTP API

### 健康 / 元信息

#### `GET /v1/health`

**响应**

```json
{ "status": "ok", "version": "0.1.0" }
```

---

### 会话

#### `POST /v1/sessions/init`

幂等创建。当 `client_session_id` 已存在时返回原 `memory_session_id`，不会重新生成。

**请求**

```json
{
  "client": "claude_code",
  "client_session_id": "abc-123",
  "project": "cses-40",
  "model": "claude-sonnet-4-6",
  "user_prompt": "可选 — 第一条用户消息"
}
```

**响应**

```json
{ "memory_session_id": "5f8a-..." }
```

#### `POST /v1/sessions/complete`

**请求**

```json
{ "memory_session_id": "5f8a-...", "status": "completed" }
```

`status`：`completed` / `failed`

**响应**

```json
{ "ok": true }
```

---

### 观察（Observations）

观察分两条写入路径：直写（已结构化）和入队（异步 LLM 提取）。

#### `POST /v1/observations` — 直写

**请求**

```json
{
  "memory_session_id": "5f8a-...",
  "client": "manual",
  "model": null,
  "project": "cses-40",
  "type": "discovery",
  "title": "短标题",
  "subtitle": "可选副标题",
  "narrative": "可选叙事段",
  "facts": ["事实 1", "事实 2"],
  "concepts": ["scrollbar", "directive"],
  "files_read": ["src/foo.ts"],
  "files_modified": ["src/bar.ts"],
  "prompt_number": 1
}
```

`type` 取值：`bugfix` `feature` `refactor` `change` `discovery` `decision`

**响应**

```json
{ "id": 505, "deduped": false }
```

去重逻辑：30 秒内同 `(client, project, type, title)` 组合视为重复，返回 `id: null` + `deduped: true`。

#### `POST /v1/observations/pending` — 入队异步提取

通常由 hooks 自动调用，不会直接成为 observation，需要后台 worker 调 LLM 处理。

**请求**

```json
{
  "memory_session_id": "5f8a-...",
  "client": "claude_code",
  "model": "claude-haiku-4-5-20251001",
  "tool_name": "Edit",
  "tool_input": "{...}",
  "tool_response": "{...}",
  "cwd": "/Users/me/proj",
  "prompt_number": 3
}
```

**响应**

```json
{ "pending_id": 9912 }
```

#### `GET /v1/observations/{id}`

**响应**

```json
{
  "id": 504,
  "memory_session_id": "5f8a-...",
  "client": "claude_code",
  "model": "claude-haiku-4-5-20251001",
  "project": "cses-40",
  "type": "change",
  "title": "Updated presentation layer documentation in CLAUDE.md",
  "subtitle": "...",
  "narrative": "...",
  "facts": ["..."],
  "concepts": ["..."],
  "files_read": ["..."],
  "files_modified": ["..."],
  "discovery_tokens": 1234,
  "created_at_epoch": 1745718450123,
  "created_at": "2026-04-27T02:47:30Z"
}
```

---

### 总结（Summaries）

#### `POST /v1/summaries/pending`

每次 `Stop` 事件入队一条会话总结，由 worker 调 LLM 抽取 `request / investigated / learned / completed / next_steps` 五个字段。

**请求**

```json
{
  "memory_session_id": "5f8a-...",
  "client": "claude_code",
  "last_user_message": "...",
  "last_assistant_message": "...",
  "prompt_number": 12
}
```

**响应**

```json
{ "pending_id": 9913 }
```

---

### 原文（Raw Conversations）

#### `POST /v1/raw`

未结构化的全文备份，用于事后追溯（claude-mem 没这个，PolyMem 复用了 MemPalace 的概念）。

**请求**

```json
{
  "memory_session_id": "5f8a-...",
  "client": "claude_code",
  "model": "claude-sonnet-4-6",
  "role": "user",
  "content": "原始消息内容",
  "tool_name": "Edit",
  "tool_input": "{...}",
  "tool_response": "{...}",
  "prompt_number": 3
}
```

**响应**

```json
{ "id": 7788 }
```

#### `GET /v1/raw/session/{memory_session_id}`

按时间升序返回某会话的所有原文消息。

**Query**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `limit` | int | 200 | 上限条数 |

**响应**

```json
{
  "messages": [
    {
      "id": 7788,
      "client": "claude_code",
      "model": "claude-sonnet-4-6",
      "role": "user",
      "content": "...",
      "tool_name": null,
      "prompt_number": 1,
      "created_at_epoch": 1745718450123
    }
  ]
}
```

---

### 上下文（$PMEM 注入块）

#### `GET /v1/context`

生成 SessionStart hook 注入用的 markdown。**支持三档体积控制**。

**Query**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `project` | string | — | **必填**。项目名（cwd basename） |
| `client` | string | null | 过滤客户端：`claude_code` / `cursor` / ... |
| `max_obs` | int | 50 | 上限条数 |
| `lite` | bool | false | 轻量模式：仅标题，不展开 narrative，不带 summary 详情 |
| `days` | int | null | 仅取最近 N 天 |
| `show_summary` | bool | true | 是否附最后一条 session_summary（lite 时强制 false） |

**典型用法**

```bash
# 完整版（与 claude-mem $CMEM 等效）— 约 3000+ tokens
curl "http://127.0.0.1:37700/v1/context?project=cses-40"

# 混合模式 — 约 300-700 tokens（推荐 SessionStart 注入）
curl "http://127.0.0.1:37700/v1/context?project=cses-40&lite=true&days=3&max_obs=30"

# 跨客户端时间线 — 不限 client
curl "http://127.0.0.1:37700/v1/context?project=cses-40&max_obs=20"
```

**响应**

```json
{
  "context": "# $PMEM cses-40 2026-04-27T02:47:35  (lite index)\n\n..."
}
```

**Lite 模式的输出结构**

```
# $PMEM <project> <ISO时间>  (lite index)

Legend: 🔴bugfix 🟣feature 🔄refactor ✅change 🔵discovery ⚖️decision
Format: ID TIME CLIENT TYPE TITLE
Fetch details via MCP: memory_get / memory_search / memory_recall_full

Stats: 30 obs (1,194t read) | 351,794t work | 99% savings

### Apr 27, 2026
504 2:47am [cl] ✅ Updated presentation layer documentation
503 2:46am [cl] ✅ ...
```

---

### 搜索

#### `GET /v1/search`

FTS5 全文 + ChromaDB 向量混合检索。

**Query**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | string | — | **必填** |
| `project` | string | null | |
| `client` | string | null | |
| `type` | string | null | observation 类型过滤 |
| `limit` | int | 20 | |
| `mode` | string | `hybrid` | `fts` / `semantic` / `hybrid` |

**模式说明**

- `fts`：SQLite FTS5 关键词匹配，最快最精确，适合知道关键词时。
- `semantic`：ChromaDB 向量相似度，适合自然语言模糊检索。
- `hybrid`：两者并行 + 倒数排名归一（reciprocal rank merge），权重 `0.4 fts + 0.6 semantic`。**默认推荐**。

**示例**

```bash
curl "http://127.0.0.1:37700/v1/search?query=virtual+scrollbar&mode=hybrid&limit=5"
```

**响应（hybrid）**

```json
{
  "mode": "hybrid",
  "results": [
    {
      "id": 500,
      "client": "claude_code",
      "type": "refactor",
      "title": "ScrollbarService Architecture Refactored from Directive to Service Pattern",
      "subtitle": "...",
      "fts_score": 0.5,
      "semantic_score": 0.83,
      "hybrid_score": 0.698,
      "source": "both"
    }
  ]
}
```

---

### 向量库

#### `POST /v1/reindex`

从 SQLite 全量重建 ChromaDB 索引（恢复 / 首次冷启动用）。无 body。

**响应**

```json
{ "indexed": 504, "vector_count": 504 }
```

#### `GET /v1/vector/stats`

```json
{ "count": 504 }
```

---

### 知识图谱

KG 来自 MemPalace，存储 `(subject, predicate, object)` 时间三元组，支持 `valid_from / valid_to` 时态有效性。

#### `POST /v1/kg/add`

**请求**

```json
{
  "subject": "polymem",
  "predicate": "uses",
  "object": "ChromaDB",
  "valid_from": "2026-04-22",
  "confidence": 1.0,
  "source": "manual"
}
```

**响应**

```json
{ "triple_id": 41 }
```

#### `POST /v1/kg/invalidate`

软失效一条三元组（设置 `valid_to`），不删除历史。

**请求**

```json
{
  "subject": "polymem",
  "predicate": "uses",
  "object": "ChromaDB",
  "ended": "2026-05-01"
}
```

**响应**

```json
{ "invalidated": 1 }
```

#### `GET /v1/kg/query`

**Query**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `entity` | string | — | **必填** |
| `as_of` | string | null | ISO 日期，仅返回该时刻有效的三元组 |
| `direction` | string | `both` | `outgoing` / `incoming` / `both` |

**响应**

```json
{
  "entity": "polymem",
  "triples": [
    {
      "subject": "polymem",
      "predicate": "uses",
      "object": "ChromaDB",
      "valid_from": "2026-04-22",
      "valid_to": null,
      "confidence": 1.0
    }
  ]
}
```

#### `GET /v1/kg/timeline`

**Query**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `entity` | string | null | 不传则返回全图时间线 |

#### `GET /v1/kg/stats`

```json
{
  "entities": 42,
  "triples": 88,
  "active_triples": 80,
  "top_predicates": [
    { "predicate": "uses", "count": 12 },
    { "predicate": "modifies", "count": 9 }
  ]
}
```

---

### 日报

#### `GET /v1/report`

把指定日期的 observations 聚合成一份工作日报。按文件路径或 concept 自动主题聚类。

**Query**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `date` | string | 今日 | `YYYY-MM-DD` |
| `project` | string | null | |
| `client` | string | null | |
| `format` | string | `markdown` | `markdown` / `json` |
| `group_by_theme` | bool | true | 按文件路径聚类（≥3 条且共享路径时生效） |

**示例**

```bash
# 今日全部客户端
curl "http://127.0.0.1:37700/v1/report" | jq -r '.report'

# 指定日期 + 项目过滤
curl "http://127.0.0.1:37700/v1/report?date=2026-04-22&project=cses-40"

# 结构化版本（用于二次加工）
curl "http://127.0.0.1:37700/v1/report?format=json"
```

或直接走脚本：

```bash
polymem report                  # 今日
polymem report 2026-04-22       # 指定日期
polymem report "" cses-40       # 今日 + 项目过滤
```

**Markdown 响应（节选）**

```markdown
# 2026-04-27 工作日报

## 今日概览
🟣 5 功能实现 · 🔴 2 Bug 修复 · 🔄 4 重构 · ✅ 8 变更

> 19 条观察 · 351,794 tokens consumed

## 🔄 重构 (4)

### scrollbar
- **#500** ScrollbarService Architecture Refactored from Directive to Service Pattern
- **#497** VirtualScrollbarDirective Migrated to ScrollbarService Architecture

## 📅 明日计划
*（从最近的 session summaries 提取）*
- 等待用户决定是否保留 ScrollbarService 在 public API
```

**JSON 响应**

```json
{
  "date": "2026-04-27",
  "project": null,
  "client": null,
  "total_observations": 19,
  "total_tokens": 351794,
  "type_counts": { "refactor": 4, "change": 8, "feature": 5, "bugfix": 2 },
  "groups_by_type": {
    "refactor": {
      "scrollbar": [{"id": 500, "title": "...", "subtitle": "..."}],
      "documentation": [{"id": 501, "title": "..."}]
    }
  },
  "next_steps": ["..."]
}
```

---

## MCP 工具

模型通过 stdio MCP 调用，注册位置：`~/.claude/settings.json` 的 `mcpServers.polymem`。

| 工具 | 说明 | 必填参数 |
|------|------|---------|
| `memory_search` | 跨客户端混合搜索 | `query` |
| `memory_get` | 按 ID 批量取详情 | `ids: number[]` |
| `memory_context` | 取 `$PMEM` 块 | `project` |
| `memory_kg_query` | 知识图谱查询 | `entity` |
| `memory_recall_full` | 取某会话原文 | `memory_session_id` |

### `memory_search`

```json
{
  "query": "scrollbar refactor",
  "project": "cses-40",
  "client": "claude_code",
  "type": "refactor",
  "limit": 10
}
```

`type` 枚举：`bugfix` `feature` `refactor` `change` `discovery` `decision`

返回：与 `GET /v1/search?mode=hybrid` 相同的 JSON 字符串。

### `memory_get`

```json
{ "ids": [500, 501, 502] }
```

返回完整 observation 数组。

### `memory_context`

```json
{
  "project": "cses-40",
  "client": "claude_code",
  "max_obs": 30
}
```

⚠ 当前 MCP `memory_context` 透传未带 `lite` / `days` 参数。**混合模式注入由 hook 直接调 HTTP 完成**（`polymem/cli/hook.py` 的 `context-lite` 命令），MCP 这层暂时只能拉完整版。

### `memory_kg_query`

```json
{
  "entity": "polymem",
  "as_of": "2026-04-27",
  "direction": "outgoing"
}
```

### `memory_recall_full`

```json
{
  "memory_session_id": "5f8a-...",
  "limit": 100
}
```

---

## 环境变量

引擎进程：

| 变量 | 默认 | 作用 |
|------|------|------|
| `POLYMEM_HOST` | `127.0.0.1` | 监听地址 |
| `POLYMEM_PORT` | `37700` | 监听端口 |
| `POLYMEM_DATA_DIR` | `~/.polymem` | 数据目录 |
| `POLYMEM_PROVIDER` | `claude_cli` | LLM provider：`claude_cli` / `openrouter` / `ollama` / `anthropic` |
| `POLYMEM_MODEL` | `claude-haiku-4-5-20251001` | 提取模型 |
| `POLYMEM_API_KEY` | — | provider=非 claude_cli 时需要 |

Hook 进程（`polymem/cli/hook.py`）：

| 变量 | 默认 | 作用 |
|------|------|------|
| `POLYMEM_BASE_URL` | `http://127.0.0.1:37700` | 引擎地址 |
| `POLYMEM_ROOT` | `~/demo/polymem` | hooks.json 自动注入 |
| `POLYMEM_LITE_DAYS` | `3` | 混合模式注入的时间窗口 |
| `POLYMEM_LITE_MAX` | `30` | 混合模式注入的最大条数 |

MCP 进程（`polymem/cli/mcp_server.py`）：

| 变量 | 默认 | 作用 |
|------|------|------|
| `POLYMEM_BASE_URL` | `http://127.0.0.1:37700` | 引擎地址 |

---

## Codex CLI 集成

Codex CLI **不暴露 hooks**，但每个会话完整写到 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`。PolyMem 通过 watcher 进程轮询这些文件、增量推送给引擎。

**架构**

```
Codex CLI 写 JSONL
       ↓ (每 30s)
polymem codex (后台进程)
       ↓ HTTP POST
PolyMem Engine :37700
       ↓
  /v1/observations/pending  /v1/summaries/pending
```

**JSONL → PolyMem 映射**

| Codex 记录 | PolyMem 端点 |
|-----------|-------------|
| `session_meta`（首行） | `POST /v1/sessions/init`（client=codex, client_session_id=payload.id） |
| 配对 `function_call` + `function_call_output`（按 call_id） | `POST /v1/observations/pending` |
| `event_msg / task_complete` + 最近 user/agent | `POST /v1/summaries/pending` |

**启动**

```bash
# 注册 MCP 读层（一次性，需要重启 Codex 生效）
cat >> ~/.codex/config.toml <<'EOF'
[mcp_servers.polymem]
type = "stdio"
command = "polymem"
args = ["mcp"]
EOF

# 启动采集 watcher（前台或后台）
polymem codex                # foreground
nohup polymem codex &   # background
```

**状态查看**

```bash
# Watcher 进程
ps aux | grep codex/watcher | grep -v grep

# 处理进度（每个 session 文件的 byteOffset）
cat ~/.polymem/codex-watcher-state.json | python3 -m json.tool

# Codex 数据量
sqlite3 ~/.polymem/polymem.db "SELECT COUNT(*) FROM sessions WHERE client='codex'"
sqlite3 ~/.polymem/polymem.db "SELECT message_type, status, COUNT(*) FROM pending_messages WHERE client='codex' GROUP BY message_type, status"

# 实时日志
tail -f ~/.polymem/codex-watcher.log
```

**环境变量**

| 变量 | 默认 | 作用 |
|------|------|------|
| `POLYMEM_CODEX_POLL_MS` | `30000` | 轮询间隔（ms） |
| `POLYMEM_CODEX_BACKFILL_DAYS` | `1` | 只处理最近 N 天的 session 文件（防止首次启动时大量历史回灌） |

**注意事项**

- Watcher 与 Claude Code hook **共用同一引擎、同一 SQLite**，跨客户端时间线天然合并
- 首次启动若 `BACKFILL_DAYS` 较大，可能产生大量 pending（每条 ~10 秒走 claude_cli 提取），建议默认从 1 天开始
- State 文件 `~/.polymem/codex-watcher-state.json` 记录每个 JSONL 的 byte offset，重启幂等
- 写入中的 JSONL 末尾可能是半行，watcher 只消费到最后完整换行符——下次轮询补齐
- `update_plan` 工具默认在 SKIP_TOOLS 列表（噪声大），可改 `polymem/cli/codex_watcher.py` 顶部增删

**回退**

```bash
# 停 watcher
pkill -f polymem codex

# 删 MCP 注册（手动编辑 ~/.codex/config.toml 删除 [mcp_servers.polymem] 段）
# 或还原备份：
ls ~/.codex/config.toml.bak.*
cp ~/.codex/config.toml.bak.YYYYMMDD-HHMMSS ~/.codex/config.toml

# 重置状态（重新从头处理，会创建重复 session 但 dedup 会兜住）
rm ~/.polymem/codex-watcher-state.json
```

---

## Hooks 模式

| 模式 | 安装命令 | SessionStart 注入 | 体积 | 适合场景 |
|------|----------|------------------|------|----------|
| 纯采集 | `install-claude-code.sh` | ❌ | 0 | 还在验证记忆质量 |
| 混合 | `install-claude-code.sh --hybrid` | ✅ 轻量索引 | ~300-700t | **推荐**：标题级注入 + MCP 深挖 |
| 完整 | `install-claude-code.sh --with-injection` | ✅ 完整 $PMEM | ~3000+t | 需要丰富上下文 |

切换模式：

```bash
polymem install uninstall-claude-code
polymem install claude-code --mode hybrid
```

每次安装都会自动备份 `~/.claude/settings.json` 到 `.bak.YYYYMMDD-HHMMSS`。

---

## 常见错误

### `404 observation not found`

`GET /v1/observations/{id}` 找不到记录。可能 ID 不存在或已被去重未真正落表。

### `400 detail: ...`

参数校验失败。常见原因：

- KG `add_triple` 时 `subject`/`predicate`/`object` 为空字符串
- KG 时间字段非法 ISO 格式
- `type` 取值不在白名单

### `500` 持续 5 秒以上

worker 卡死或 LLM 超时。排查：

```bash
tail -100 ~/.polymem/engine.log
sqlite3 ~/.polymem/polymem.db "SELECT status, COUNT(*) FROM pending_messages GROUP BY status"
```

`pending` 长期不减表示 worker 没消化；`failed` 多表示 LLM 调用失败（claude_cli 模式下通常是登录态失效）。

### Hook 未触发

```bash
jq '.hooks | keys' ~/.claude/settings.json
ps aux | grep "engine.server" | grep -v grep
```

无 polymem 相关 hook → 重新装：`install-claude-code.sh --hybrid`
引擎未运行 → `polymem engine`

---

*最后更新：2026-04-27 · PolyMem v0.1.0*
