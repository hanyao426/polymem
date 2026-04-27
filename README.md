# @ccc/polymem

> 跨工具记忆系统 · Claude Code / Codex / Cursor / Cline 共用一份记忆。
> 自动采集工具调用 → LLM 提炼为结构化 observation → FTS5 + 向量混合检索 → 通过 MCP 暴露给所有客户端。

---

## 这是什么

你在 Claude Code 里改了一个 bug、在 Codex 里重构了一个模块、隔几天又在 Cursor 里碰到相关代码——三个工具**互相不知道对方做过什么**。

PolyMem 解决这个：

- 在每个客户端上挂一个轻量采集器（Hook 或 Watcher），把工具调用的输入输出写到统一存储
- 后台 LLM 把原始 tool call 提炼成结构化记录（类型 / 标题 / 关键事实 / 改动文件）
- 提供搜索 + MCP 接口，任何客户端都能问"这事我们之前怎么处理的"

```
   Claude Code     Codex CLI       Cursor / Cline / ...
       │              │                   │
       │ hooks        │ JSONL watcher     │ (待补)
       ▼              ▼                   ▼
   ┌────────────────────────────────────────────────┐
   │   Engine (Python · :37700)                     │
   │   ├─ SQLite + FTS5  (主库)                     │
   │   ├─ ChromaDB       (向量)                     │
   │   ├─ KG SQLite      (时间三元组)               │
   │   └─ ExtractionWorker (后台 LLM 提取)          │
   └────────────────────────────────────────────────┘
       ▲
       │ MCP stdio
   ┌─────────────┐
   │ MCP Server  │  ← 任何 MCP 客户端都能挂
   │  (5 个工具)  │
   └─────────────┘
```

---

## 架构注意

PolyMem 由两半组成：

| 部分 | 语言 | 装在哪 |
|-----|------|-------|
| **Engine**（HTTP 服务、存储、LLM 提取、搜索） | Python | `pip install` |
| **Collectors + MCP server**（每个客户端的适配器） | TypeScript | `npm install -g @ccc/polymem` |

**两半都要装**。npm 包不内嵌 Python 引擎——这是有意为之，避免 npm postinstall 跑 pip 的脆弱链路。

---

## 一、装

### 1.1 装 Engine（Python）

```bash
git clone <repo> ~/demo/polymem    # 把源码 clone 到任意位置
cd ~/demo/polymem
python3 -m venv .venv
.venv/bin/pip install -e .
```

依赖：`fastapi` `uvicorn` `chromadb`，自动从 `pyproject.toml` 拉。需要 Python 3.10+。

### 1.2 装 CLI（npm）

```bash
npm install -g @ccc/polymem
```

之后全局有 `polymem` 命令。需要 Node 18+。

### 1.3 体检

```bash
polymem doctor
```

应该看到 7 条检查（engine、tsx、源码三件套、Claude Code hook、Codex MCP）。引擎没起就是 `✗`，正常。

---

## 二、启动 Engine

```bash
polymem engine
# 或者直接：~/demo/polymem/scripts/start-engine.sh
```

输出应该是：

```
▶ PolyMem engine starting on 127.0.0.1:37700
  provider: claude_cli
  model:    claude-haiku-4-5-20251001
  data:     /Users/<you>/.polymem/
```

LLM provider 默认 `claude_cli`——通过 `claude` 命令子进程走你已登录的 Claude Code 订阅，**零 API key**。其他可选：`anthropic` / `openrouter` / `ollama`，需要配 `POLYMEM_API_KEY`。

后台跑：

```bash
nohup polymem engine > ~/.polymem/engine.log 2>&1 &
```

健康检查：

```bash
curl http://127.0.0.1:37700/v1/health
# {"status":"ok","version":"0.1.0"}
```

---

## 三、接入 Claude Code

### 3.1 装 hooks（三档模式选一）

```bash
# 推荐：混合模式 — 自动采集 + SessionStart 注入轻量 $PMEM 索引（~300 token）
polymem install-claude-code --hybrid

# 只采集，不注入（怕污染上下文时用）
polymem install-claude-code

# 完整注入（每个新会话注入完整 $PMEM 块，~3000 token）
polymem install-claude-code --with-injection
```

脚本会**追加** hook 到 `~/.claude/settings.json`，不会覆盖你已有的 hooks。每次执行前自动备份到 `.bak.YYYYMMDD-HHMMSS`。

### 3.2 注册 MCP（让模型能主动查记忆）

编辑 `~/.claude/settings.json`，在 `mcpServers` 字段加：

```json
{
  "mcpServers": {
    "polymem": {
      "command": "polymem",
      "args": ["mcp"]
    }
  }
}
```

### 3.3 验证

**新开**一个 Claude Code 会话（hooks 和 MCP 都在 session 启动时加载，当前会话不会热加载）。

如果你装的是 `--hybrid` 或 `--with-injection`，会话顶部应该有这样一行：

```
SessionStart:startup says: [PolyMem] injected $PMEM lite index — 12 obs, ~340 tokens, last 3d (project=xxx). Use MCP memory_search/memory_get for details.
```

手动确认 MCP：在 Claude Code 里输入 `/mcp`，列表里应该有 `polymem`。

### 3.4 卸载

```bash
polymem uninstall-claude-code
```

只删带 `polymem` 关键字的 hook 行，其他 hook 保留。带备份。

---

## 四、接入 Codex

Codex CLI 没有 hook 系统，但每个会话完整写到 JSONL。所以 Codex 这边是 **Watcher 进程**而非 hook。

### 4.1 启动 Watcher（采集端）

```bash
polymem codex --background
# log: ~/.polymem/codex-watcher.log
```

每 30 秒扫一次 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，按 byte-offset 增量推给 engine。重启幂等。

停止：

```bash
pkill -f codex/watcher.ts
```

### 4.2 注册 MCP（读端）

编辑 `~/.codex/config.toml`，**追加**到末尾：

```toml
[mcp_servers.polymem]
type = "stdio"
command = "polymem"
args = ["mcp"]
```

### 4.3 让 Codex 主动用记忆（关键）

Codex 没有 SessionStart 自动注入机制，必须靠 `AGENTS.md` 全局规则**强制**模型先查记忆。在 `~/.codex/AGENTS.md` 加：

```markdown
## 记忆系统使用规则

接入 PolyMem MCP（`polymem`）。开始任务前满足任一条件就先调 `memory_search`：

- 用户提到"上次/之前/继续"等指向过去的词
- 任务涉及已存在的代码模块（不是从零新建）
- 用户描述的问题听起来已经讨论或修过
- 用户提到具体文件路径但你不知道历史

调用顺序：先 `memory_search` 找 ID → 再 `memory_get` 拿详情 → 再开始动手。
引用记忆时注明 client（`claude_code` / `codex` / 等）来源。
```

完整版（涵盖工具列表、执行模式、失败兜底等）参考 [API.md](./API.md#codex-cli-集成)。

### 4.4 验证

**重启** Codex（重新加载 MCP）。在新会话里说一句这种话试试：

```
之前 ScrollbarService 那次重构是怎么决策的？
```

如果 Codex 调 `memory_search("ScrollbarService refactor")` 拉到结果，并且引用时说"之前在 Claude Code 里..."，就证明跨工具读通了。

---

## 五、日常用法

### 5.1 命令行查询

```bash
# 健康 + 数据量
polymem doctor

# 今天的日报
polymem report

# 指定日期 + 项目
polymem report 2026-04-22 cses-client

# 只看某个客户端
polymem report "" "" codex
```

### 5.2 HTTP 直查

```bash
# 混合搜索（FTS5 + 向量）
curl "http://127.0.0.1:37700/v1/search?query=scrollbar&limit=5"

# 按客户端过滤
curl "http://127.0.0.1:37700/v1/search?query=auth&client=codex"

# 按类型过滤
curl "http://127.0.0.1:37700/v1/search?query=bug&type=bugfix"

# 拉一段 $PMEM 上下文块
curl "http://127.0.0.1:37700/v1/context?project=my-app&lite=true&days=3"

# 单条详情
curl http://127.0.0.1:37700/v1/observations/123
```

### 5.3 SQL 直查（最快）

```bash
# 总数 + 按客户端分桶
sqlite3 ~/.polymem/polymem.db "SELECT client, COUNT(*) FROM observations GROUP BY client"

# 最近 10 条
sqlite3 ~/.polymem/polymem.db "SELECT id, client, type, title FROM observations ORDER BY id DESC LIMIT 10"

# Pending 队列状态
sqlite3 ~/.polymem/polymem.db "SELECT status, COUNT(*) FROM pending_messages GROUP BY status"
```

### 5.4 在客户端里让模型主动查（最高频用法）

不用做任何事，按你日常方式说话即可：

| 你说 | 模型自动会做 |
|------|------------|
| "继续昨天那个事" | 调 `memory_search` 找最近的相关记忆 |
| "ScrollbarService 那次重构是咋决定的" | 调 `memory_search("ScrollbarService refactor")` |
| "这 bug 是不是又出现了" | 调 `memory_search` 类型过滤 `bugfix` |

Codex 端要 `AGENTS.md` 配置过才会主动查（见 4.3）；Claude Code 端只要装了 hybrid/with-injection 模式，模型一进会话就有索引，命中率高。

---

## 六、所有 CLI 子命令

```
polymem mcp                          # 跑 MCP server（stdio，被客户端调用）
polymem codex [--background]         # Codex JSONL watcher
polymem install-claude-code [--hybrid|--with-injection]
polymem uninstall-claude-code
polymem engine                       # 启动 Python engine
polymem report [date] [project] [client]
polymem doctor                       # 体检
polymem hook <event>                 # 单个 hook 事件（hooks.json 内部用，不手动调）
polymem --help
```

---

## 七、配置

### 7.1 引擎进程

| 变量 | 默认 | 作用 |
|------|------|------|
| `POLYMEM_HOST` | `127.0.0.1` | 监听地址 |
| `POLYMEM_PORT` | `37700` | 监听端口 |
| `POLYMEM_DATA_DIR` | `~/.polymem` | 数据目录 |
| `POLYMEM_PROVIDER` | `claude_cli` | LLM provider：`claude_cli` / `anthropic` / `openrouter` / `ollama` |
| `POLYMEM_MODEL` | `claude-haiku-4-5-20251001` | 提取模型 |
| `POLYMEM_API_KEY` | — | 非 `claude_cli` 时必填 |

### 7.2 Claude Code 注入窗口

| 变量 | 默认 | 作用 |
|------|------|------|
| `POLYMEM_LITE_DAYS` | `3` | hybrid 模式注入的时间窗口 |
| `POLYMEM_LITE_MAX` | `30` | hybrid 模式注入的最大条数 |

### 7.3 Codex Watcher

| 变量 | 默认 | 作用 |
|------|------|------|
| `POLYMEM_CODEX_POLL_MS` | `30000` | 轮询间隔 ms |
| `POLYMEM_CODEX_BACKFILL_DAYS` | `1` | 只处理最近 N 天的 session 文件（防首次启动大量历史回灌） |

---

## 八、排错

### `polymem doctor` 显示 engine 不可达

```bash
# 看日志
tail -50 ~/.polymem/engine.log

# 看进程
ps aux | grep "engine.server" | grep -v grep

# 重启
pkill -f engine.server
nohup polymem engine > ~/.polymem/engine.log 2>&1 &
```

### Pending 队列堆积不消化

```bash
sqlite3 ~/.polymem/polymem.db "SELECT status, COUNT(*) FROM pending_messages GROUP BY status"
```

`pending` 长期不减 → worker 没运行 / LLM 调用失败。`failed` 多 → 多半是 `POLYMEM_PROVIDER` 配错（如配了 `openrouter` 但没 key）。

复活卡住的 message：

```bash
sqlite3 ~/.polymem/polymem.db "UPDATE pending_messages SET status='pending' WHERE status='processing'"
```

### Claude Code 新会话没看到 `[PolyMem] injected` banner

1. 确认装的是 `--hybrid` 或 `--with-injection`（纯采集模式不注入）：
   ```bash
   jq '.hooks.SessionStart' ~/.claude/settings.json
   ```
2. 必须**新开**会话——hook 不热加载

### Codex 调 `memory_search` 报 `Failed to parse JSON`

通常是 engine 那侧出错（FTS5 语法、500 等）：

```bash
tail -50 ~/.polymem/engine.log
```

把出问题的 query 直接 curl 引擎复现：

```bash
curl --get --data-urlencode "query=<出问题的查询>" http://127.0.0.1:37700/v1/search
```

### MCP 工具在客户端里看不到

- Claude Code：`/mcp` 命令查列表；settings.json 里 `mcpServers.polymem` 必须存在
- Codex：`~/.codex/config.toml` 里 `[mcp_servers.polymem]` 必须存在；**重启 Codex**

### 想完全清空数据重来

```bash
pkill -f engine.server
rm -rf ~/.polymem/
polymem engine
```

---

## 九、不在包里的东西

- **Python engine 源码**——用 `pip install` 装。npm 包只装客户端适配器。
- **Cursor / Cline / Windsurf / ChatGPT 的 collector**——只有 stub。要接入这些工具需要自己写一个文件实现 `collectors/base/collector.ts` 的 `ICollector` 接口，~100 行模板见 `collectors/codex/watcher.ts`。
- **数据迁移工具**——从 claude-mem 之类的现存系统导入数据需要自己写脚本。

---

## 十、文档地图

- `README.md`（本文）—— 用户视角的怎么装怎么用
- [API.md](./API.md) —— HTTP 端点完整参考、MCP 工具签名、Codex JSONL → PolyMem 映射
- [ARCHITECTURE.md](./ARCHITECTURE.md) —— 架构设计原理、模块来源
- [docs/COLLECTOR_SPEC.md](./docs/COLLECTOR_SPEC.md) —— 写新 collector 的规范
- [docs/ENGINE_API.md](./docs/ENGINE_API.md) —— 引擎内部 API
