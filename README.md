# PolyMem

> 跨工具记忆系统 · Claude Code / Codex / Cursor / Cline 共用一份记忆。
> 自动采集工具调用 → LLM 提炼为结构化 observation → FTS5 + 向量混合检索 → 通过 MCP 暴露给所有客户端。

**纯 Python 实现，一行命令装好。**

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
   │   Engine (FastAPI · :37700)                    │
   │   ├─ SQLite + FTS5  (主库)                     │
   │   ├─ ChromaDB       (向量)                     │
   │   ├─ KG SQLite      (时间三元组)               │
   │   └─ ExtractionWorker (后台 LLM 提取)          │
   └────────────────────────────────────────────────┘
       ▲
       │ MCP stdio
   ┌─────────────┐
   │ MCP Server  │  ← Claude Code / Codex / 任何 MCP 客户端都能挂
   │  (5 个工具)  │
   └─────────────┘
```

---

## 一、装

### 前置依赖

| # | 必备项 | 说明 |
|---|------|------|
| 1 | Python 3.10+ | 引擎运行时 |
| 2 | `pipx` | 全局安装 CLI 工具的标准方式 |
| 3 | **LLM 调用入口（二选一，否则提取 worker 全部失败）** | 见下方「LLM provider 必读」 |

```bash
brew install pipx && pipx ensurepath
```

### ⚠️ LLM provider 必读

PolyMem 的核心能力是把工具调用提炼成结构化记忆，这一步**必须**调用 LLM。默认走 `claude_cli`（subprocess 调你本机已登录的 `claude` 命令）——**这要求你装了 Claude Code 并已登录订阅**。

**如果你没有 Claude Code 订阅，装 PolyMem 之前务必先决定走哪条 LLM 路线**：

| 路线 | 设置 | 成本 |
|------|------|------|
| **Claude Code 订阅**（默认，零额外 key） | 不用配 | 已订阅 Claude Max/Team 即免费 |
| **Anthropic API** | `POLYMEM_PROVIDER=anthropic POLYMEM_API_KEY=sk-ant-...` | 按 token 计费 |
| **OpenRouter**（含免费模型） | `POLYMEM_PROVIDER=openrouter POLYMEM_API_KEY=sk-or-...` `POLYMEM_MODEL=xiaomi/mimo-v2-flash:free` | 免费模型可零成本，付费按 token |
| **本地 Ollama**（零成本零联网） | `POLYMEM_PROVIDER=ollama POLYMEM_MODEL=qwen2.5:7b` | 仅本机算力 |

**症状判别**：如果你装好之后 `polymem doctor` 一切绿但 `pending_messages` 表里 `failed` 一直涨、`processed` 不动 → 100% 是 LLM provider 没配对。

### 装 PolyMem

**直接从 git 装**（推荐）：

```bash
pipx install git+https://github.com/hanyao426/polymem.git
```

或者**本地开发态**（克隆下来要改源码）：

```bash
git clone https://github.com/hanyao426/polymem.git
cd polymem
pipx install -e .
```

装完之后全局有 `polymem` 命令。

如果你走的是 API key 路线，把 provider 写到 shell rc 里持久化：

```bash
# ~/.zshrc 或 ~/.bashrc
export POLYMEM_PROVIDER=anthropic
export POLYMEM_API_KEY=sk-ant-...
export POLYMEM_MODEL=claude-haiku-4-5-20251001
```

---

## 二、一行命令搞定全部

```bash
polymem engine &        # 启引擎（后台跑）
polymem init            # 探测 Claude Code / Codex 并自动配置
```

`polymem init` 会做：

1. 检查引擎健康
2. 探测 `~/.claude/settings.json` → 装 hybrid 模式 hooks（轻量 SessionStart 注入）+ 注册 MCP
3. 探测 `~/.codex/config.toml` → 注册 MCP + 写入 `~/.codex/AGENTS.md` 强制规则
4. 输出下一步指引

加 `-y` 静默接受所有提示（脚本场景）：

```bash
polymem init -y
```

---

## 三、手动配置（如果你不喜欢 init）

### 3.1 启动引擎

```bash
polymem engine
# 或后台：
nohup polymem engine > ~/.polymem/engine.log 2>&1 &
```

LLM provider 默认 `claude_cli`——通过 `claude` 命令子进程走你已登录的 Claude Code 订阅，**零 API key**。其他可选：`anthropic` / `openrouter` / `ollama`，需要配 `POLYMEM_API_KEY`。

健康检查：

```bash
curl http://127.0.0.1:37700/v1/health
# {"status":"ok","version":"0.1.0"}
```

### 3.2 接入 Claude Code

**装 hooks**（三档模式选一）：

```bash
polymem install claude-code --mode hybrid          # 推荐：轻量注入 ~300t
polymem install claude-code --mode collect-only    # 只采集，不注入
polymem install claude-code --mode full-injection  # 完整 $PMEM 块 ~3000t
```

脚本会**追加** hook 到 `~/.claude/settings.json`，**不覆盖**你已有的 hooks。每次自动备份到 `.bak.YYYYMMDD-HHMMSS`。

**注册 MCP**（让模型主动查记忆）：

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

**验证**：开**新**会话（hooks 和 MCP 都在 session 启动时加载）。如果装的是 hybrid 或 full-injection，会话顶部应该看到：

```
SessionStart:startup says: [PolyMem] injected $PMEM lite index — 12 obs, ~340 tokens, last 3d (project=xxx). Use MCP memory_search/memory_get for details.
```

`/mcp` 命令查 MCP 列表，应该有 `polymem`。

**卸载**：

```bash
polymem install uninstall-claude-code
```

只删带 `polymem` 关键字的 hook，其他 hook 保留。

### 3.3 接入 Codex

Codex 没有 hook 系统，所以是 **Watcher 进程**。

**配置 MCP + AGENTS.md 规则**（一次性）：

```bash
polymem install codex
```

脚本会做两件事：
- 在 `~/.codex/config.toml` 末尾追加 `[mcp_servers.polymem]`
- 在 `~/.codex/AGENTS.md` 追加"必须先查记忆"的强制规则

**启动 Watcher**（持续后台跑）：

```bash
nohup polymem codex > ~/.polymem/codex-watcher.log 2>&1 &
```

每 30 秒扫一次 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，按 byte-offset 增量推给 engine。重启幂等。停止：

```bash
pkill -f "polymem codex"
```

**验证**：重启 Codex 让 MCP 生效，然后说一句这种话：

> 之前 ScrollbarService 那次重构是怎么决策的？

如果 Codex 调 `memory_search` 拉到结果并引用"之前在 Claude Code 里..."，跨工具记忆就通了。

---

## 四、日常用法

### 命令行

```bash
polymem doctor                                # 体检（6 项）
polymem report                                # 今天的日报
polymem report 2026-04-22 cses-client         # 指定日期 + 项目
polymem report "" "" codex                    # 只看某客户端
```

### HTTP 直查

```bash
# 混合搜索（FTS5 + 向量）
curl "http://127.0.0.1:37700/v1/search?query=scrollbar&limit=5"

# 按客户端过滤
curl "http://127.0.0.1:37700/v1/search?query=auth&client=codex"

# 拉一段 $PMEM 上下文块
curl "http://127.0.0.1:37700/v1/context?project=my-app&lite=true&days=3"

# 单条详情
curl http://127.0.0.1:37700/v1/observations/123
```

### SQL 直查（最快）

```bash
sqlite3 ~/.polymem/polymem.db "SELECT client, COUNT(*) FROM observations GROUP BY client"
sqlite3 ~/.polymem/polymem.db "SELECT id, client, type, title FROM observations ORDER BY id DESC LIMIT 10"
sqlite3 ~/.polymem/polymem.db "SELECT status, COUNT(*) FROM pending_messages GROUP BY status"
```

### 在客户端里让模型主动查（最高频用法）

不用做任何事，按你日常方式说话即可：

| 你说 | 模型自动会做 |
|------|------------|
| "继续昨天那个事" | 调 `memory_search` 找最近的相关记忆 |
| "ScrollbarService 那次重构是咋决定的" | 调 `memory_search("ScrollbarService refactor")` |
| "这 bug 是不是又出现了" | 调 `memory_search` + `type=bugfix` 过滤 |

Codex 端要 `polymem install codex` 配置过才会主动查；Claude Code 端只要装了 hybrid/full-injection 模式，模型一进会话就有索引。

---

## 五、所有 CLI 子命令

```
polymem engine                                    # 启动 Python 引擎
polymem mcp                                       # 跑 MCP server（stdio，被客户端调用）
polymem codex                                     # Codex JSONL watcher
polymem hook <event>                              # 单个 hook 事件（hooks.json 内部用，不手动调）
polymem install claude-code [--mode hybrid|collect-only|full-injection]
polymem install uninstall-claude-code
polymem install codex
polymem init [-y]                                 # 一键探测+配置
polymem doctor                                    # 6 项体检
polymem report [date] [project] [client]
polymem --help
```

---

## 六、配置（环境变量）

### 引擎进程

| 变量 | 默认 | 作用 |
|------|------|------|
| `POLYMEM_HOST` | `127.0.0.1` | 监听地址 |
| `POLYMEM_PORT` | `37700` | 监听端口 |
| `POLYMEM_DATA_DIR` | `~/.polymem` | 数据目录 |
| `POLYMEM_PROVIDER` | `claude_cli` | LLM provider：`claude_cli` / `anthropic` / `openrouter` / `ollama` |
| `POLYMEM_MODEL` | `claude-haiku-4-5-20251001` | 提取模型 |
| `POLYMEM_API_KEY` | — | 非 `claude_cli` 时必填 |

### Claude Code 注入窗口

| 变量 | 默认 | 作用 |
|------|------|------|
| `POLYMEM_LITE_DAYS` | `3` | hybrid 模式注入的时间窗口 |
| `POLYMEM_LITE_MAX` | `30` | hybrid 模式注入的最大条数 |

### Codex Watcher

| 变量 | 默认 | 作用 |
|------|------|------|
| `POLYMEM_CODEX_POLL_MS` | `30000` | 轮询间隔 ms |
| `POLYMEM_CODEX_BACKFILL_DAYS` | `1` | 只处理最近 N 天的 session（防首次启动大量历史回灌） |

---

## 七、排错

### `polymem doctor` 显示 engine 不可达

```bash
tail -50 ~/.polymem/engine.log              # 看日志
ps aux | grep polymem | grep -v grep        # 看进程
pkill -f "polymem engine"                   # 重启
nohup polymem engine > ~/.polymem/engine.log 2>&1 &
```

### Pending 队列堆积不消化

```bash
sqlite3 ~/.polymem/polymem.db "SELECT status, COUNT(*) FROM pending_messages GROUP BY status"
```

`pending` 长期不减 → worker 没运行 / LLM 调用失败。`failed` 多 → 多半是 `POLYMEM_PROVIDER` 配错。

复活卡住的 message：

```bash
sqlite3 ~/.polymem/polymem.db "UPDATE pending_messages SET status='pending' WHERE status='processing'"
```

### Claude Code 新会话没看到 `[PolyMem] injected` banner

1. 确认装的是 `--mode hybrid` 或 `--mode full-injection`：
   ```bash
   jq '.hooks.SessionStart' ~/.claude/settings.json
   ```
2. 必须**新开**会话——hook 不热加载

### Codex 调 `memory_search` 报错

通常是引擎那侧出错（FTS5 语法、500 等）：

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
pkill -f "polymem (engine|codex)"
rm -rf ~/.polymem/
polymem engine
```

---

## 八、当前状态 / 不在包里的东西

- ✅ Claude Code：完整支持（hooks + MCP）
- ✅ Codex CLI：完整支持（watcher + MCP + AGENTS.md）
- ⏸️ Cursor / Cline / Windsurf / ChatGPT Desktop：未来扩展，需要为每个工具写一个 Watcher（或 Hook，看其能力）。模板见 `polymem/cli/codex_watcher.py`，~200 行
- ⏸️ 数据迁移工具：从 claude-mem 之类的现存系统导入需要自己写脚本

---

## 九、文档地图

- `README.md`（本文）—— 用户视角的怎么装怎么用
- [API.md](./API.md) —— HTTP 端点完整参考、MCP 工具签名、Codex JSONL → PolyMem 映射

---

## 十、内部架构（懒得看可跳过）

```
polymem/                  # Python 包
├── server.py             # FastAPI 引擎 + ExtractionWorker 后台线程
├── db.py                 # SQLite + FTS5 schema
├── extractor.py          # 跨 provider LLM 调度（claude_cli/anthropic/openrouter/ollama）
├── context_generator.py  # $PMEM 块生成（lite + full）
├── vector_store.py       # ChromaDB 封装
├── searcher.py           # 两层检索 + BM25 重排（来自 MemPalace）
├── knowledge_graph.py    # KG SQLite（来自 MemPalace）
├── report.py             # 日报聚合（按文件路径主题聚类）
├── modes/code.json       # LLM 提取 prompt 模板（来自 claude-mem）
├── hooks/                # Claude Code hook JSON 模板（3 档）
└── cli/                  # CLI 入口
    ├── main.py           # subcommand 分发
    ├── client.py         # HTTP 客户端（stdlib only，hook 冷启动 ~80ms）
    ├── hook.py           # Claude Code hook 处理器
    ├── codex_watcher.py  # Codex JSONL watcher 守护进程
    ├── mcp_server.py     # MCP stdio server（5 个读工具）
    └── install.py        # 装/卸 Claude Code hooks + Codex 配置
```

工程感想：[ARCHITECTURE.md](./ARCHITECTURE.md) 的初版讲了"为什么 Collector pattern" + 复用 claude-mem / MemPalace 的取舍。这个文档现在已经过时（v0.1 全 Python 化时合并到 README），但思路还能看。
