# QMemory

QMemory 是一个本地优先、项目隔离、可跨设备合并的 coding-agent 记忆内核。它面向 Codex、Claude Code、Cursor 等客户端，但不把任何一个智能体或模型当作数据真相。

当前是 `v0.11.0`：QMemory 已形成 Codex + Claude Code 的本地任务档案库和可验收记忆工作台。它先无损保留每份原始会话，再按项目把主会话、跨 Agent 来源副本和子 Agent 执行分支组织为一项任务；新增项目显式绑定、worktree 归并、索引脱敏、同步 run ID、质量发布闸门与 MemoryCore 影子基准。

`v0.11.0` 将 TencentDB MemoryCore 固定在安全的 `shadow` 模式：QMemory 继续作为原始会话和已确认判断的唯一真相；MemoryCore 只接收脱敏的 L0 副本，只生成候选和召回计划，不自动确认、不自动注入。连接、多设备身份和安全配置见 [MemoryCore 集成指南](docs/memorycore-integration.md)，质量路线图见 [v0.11 质量路线图](docs/v0.11-quality-roadmap.md)。

新版界面不再把“同步、候选、正式记忆”混在一个操作区：左侧持续导航负责工作台、项目库和记忆审阅；工作台默认显示项目、任务、原始会话与正式记忆四个真实指标。`v0.9.0` 将记忆图谱提升为工作台默认视图，以可点击节点展示 `项目 → 任务 → Agent 来源 → 子 Agent 分支 → L0/L1/L2/L3` 的来源链；点击任务、来源或记忆层会直接联动右侧证据详情。列表视图仍保留，用于搜索和高密度浏览。

`v0.8.0` 的隔离验证桥仍保留作为离线诊断工具；正常使用请通过“设置与连接”启用正式增量桥。

任务详情默认选择内容覆盖最完整的来源，并允许切换查看每一份来源和子 Agent 分支。来源之间存在独有内容时会显示覆盖率提示；同源会话只有在首条用户消息一致且较小会话内容至少 95% 被覆盖时才自动合并，避免把真正不同的续聊误判为副本。

项目库会从 Agent 会话自动发现项目。同一 Git remote 的仓库、worktree 和分支目录会归入同一个项目；普通文件夹保持独立。用户可以重命名项目、指定主目录、暂停自动整理或忽略误识别项目。

桌面应用每 5 分钟扫描一次 Codex 与 Claude Code 的全部本地会话，因此无需逐个打开项目。原始会话会按项目增量归档；开启“自动整理全部项目”后，后台队列会继续从所有未暂停项目中逐段提炼候选洞察。当前打开的项目只影响界面正在查看的内容，不影响其他项目同步。

同一任务被另一个 Agent 导入时，QMemory 会在产品视图中自动合并为一项工作，并只提炼一次；每个来源的原始 JSONL 仍完整保留。界面显示“可读内容数”和“原始记录数”，工具调用轨迹默认折叠，因此不同 Agent 的原始消息数无需相等。

## 直接使用本地软件

macOS 桌面应用已经生成：

```text
artifacts/QMemory.app
```

在 Finder 中双击即可使用。首页会根据真实状态一次只提示当前要做的一步：

1. 点击“安装到应用程序”，把 QMemory 固定到 `/Applications/QMemory.app`；
2. 点击“选择项目”，选一个代码仓库或普通文件夹；
3. 点击“连接 Codex”，预览并确认后把应用自身注册为本地 MCP 服务；Claude Code 使用下文的 CLI 命令连接；
4. 点击“启用自动记忆”，安装全局 Agent 协作协议；
5. QMemory 每 5 分钟自动增量归档 Codex 与 Claude Code 原始会话；
6. 默认手动点击“提炼新增会话”；也可主动开启“自动提炼”。后台每次只处理 1 段，失败可断点重试；
7. 正常使用 Codex 或 Claude Code。Agent 会在任务开始同步会话并读取当前记忆，过程中沉淀，结束时写交接。

桌面端是审阅与纠错后台，不是主写入入口。你亲手补充的记忆默认立即生效；Agent 自己推断的新判断默认“待确认”，只有你明确批准的判断才进入默认召回。事实发生变化时 Agent 会先搜索并使用“替代”，旧版本会保留。

常用快捷键：`⌘N` 写记忆、`⌘O` 选择项目、`⌘F` 搜索、`⌘R` 刷新。最近使用的项目会保留在切换菜单中。

关闭主窗口后，QMemory 会留在 macOS 菜单栏；从菜单栏可以重新打开或立即同步。Codex 使用独立的 `--mcp` 进程，因此不要求主窗口一直显示。

当前 `.app` 是 arm64 本机构建和临时签名，适用于这台 Apple Silicon Mac。它尚未使用 Apple Developer 身份公证；若 macOS 首次阻止打开，在 Finder 中右键 QMemory →“打开”并确认。对外分发前需要正式签名与公证。

## 为什么不直接同步 SQLite

每台设备拥有一个只由自己追加的文件：

```text
QMEMORY_HOME/
├── config/device.json
├── events/
│   ├── macbook-xxxx.jsonl
│   └── desktop-yyyy.jsonl
├── sources/codex/
│   ├── raw/                    # Codex rollout 原始字节，本机私有
│   └── versions/               # 源文件重写时保留旧版本
├── sources/claude-code/
│   ├── raw/                    # Claude Code 主会话 JSONL，本机私有
│   └── versions/
├── state/qmemory.sqlite3       # 判断投影，可从 events 重建
├── state/sources.sqlite3       # 会话消息检索索引，可从 raw 重建
└── conflicts/                  # 分叉日志隔离区
```

判断的 JSONL 事件分片是跨设备同步真相；SQLite 只是本机检索投影。原始 Agent 会话是另一层证据，目前出于体积和敏感信息风险只保存在产生它的设备，不会进入“设备同步”目录。

每个事件带有 `(device_id, sequence)`、前序哈希和自身哈希。导入时只接受已有分片的严格前缀扩展：

- 重复导入：忽略；
- 云端旧副本：忽略，不回滚本机；
- 同设备同序号但内容不同：隔离到 `conflicts/`，不自动覆盖；
- 两设备离线时同时替代同一判断：按确定性顺序保留一个当前版本，另一个归档，并在 `qmemory issues` 显示冲突。

它是可收敛的本地优先模型，不是实时协同数据库。两台设备同步后会得到相同投影。

## 判断而不只是笔记

QMemory 采用三层数据逻辑：

1. **输入层——原始会话**：逐字节复制 Codex rollout 与 Claude Code 主会话 JSONL，不删减、不把 thinking/工具输出伪装成普通对话；
2. **判断层——候选洞察与当前记忆**：只提取长期有效的决定、约束、事实、事故与交接；每条派生洞察引用 `codex://...#message-序号` 或 `claude-code://...#message-序号`；
3. **应用层——给 Agent 的上下文**：默认只召回用户已确认的当前记忆，原始会话按需检索。

因此，洞察不是原始内容的替代品。换模型、改提炼规则或发现误判后，都可以回到原始会话重新生成。

每条记忆都要求以下结构：

- `holder`：谁持有这个判断；
- `subject`：判断关于谁或什么；
- `statement`：具体陈述；
- `as_of`：何时成立；
- `source_ref/source_anchor/commit_sha`：依据在哪里。

`decision` 等内容可以先处于 `proposed`，经确认成为 `active`。事实变化时用 `supersede` 建替代链，不原地改历史。检索反馈只影响排序，不会偷偷修改事实置信度。

## 本机运行

Python 3.9+ 可以运行核心 CLI。先克隆仓库并建立虚拟环境：

```bash
git clone https://github.com/qiuyiwu1989-star/qmemory.git
cd qmemory
python3 -m venv .venv
.venv/bin/pip install -e '.[desktop,dev]'

# 查看 CLI
PYTHONPATH=src python3 -m qmemory --help

# 初始化一个明确的设备身份（只需第一次）
.venv/bin/qmemory init --device-id macbook-qiu

# 记录、确认、检索
.venv/bin/qmemory remember "数据库迁移必须先备份" \
  --project /path/to/repo --type decision
.venv/bin/qmemory confirm MEMORY_ID --project /path/to/repo
.venv/bin/qmemory search "数据库迁移" --project /path/to/repo

# 预览并归档本机 Codex + Claude Code 原始会话
.venv/bin/qmemory sources-preview
.venv/bin/qmemory sources-sync
.venv/bin/qmemory conversations --project /path/to/repo
.venv/bin/qmemory insight-status --project /path/to/repo

# 修正归属并重算可重建的项目映射（不会修改原始会话）
.venv/bin/qmemory bind-project CONVERSATION_ID /path/to/repo
.venv/bin/qmemory recompute-projects

# 查看同步、去重、提炼、召回与反馈发布闸门
.venv/bin/qmemory quality-report --all-projects
.venv/bin/qmemory retrieval-stats

# MemoryCore 只允许影子模式；生成 30 任务黄金标注集并离线验收
.venv/bin/qmemory memorycore-config --enable --mode shadow --fail-open --recall-top-k 5
.venv/bin/qmemory memorycore-benchmark-template \
  --output artifacts/qmemory-real-gold-v1.json --per-cohort 10
.venv/bin/qmemory memorycore-benchmark artifacts/qmemory-real-gold-v1.json

# 健康检查
.venv/bin/qmemory doctor
```

默认数据目录是 `~/.local/share/qmemory`。测试或隔离运行可使用全局参数：

```bash
.venv/bin/qmemory --home /tmp/qmemory-test init --device-id test-device
```

## 多设备同步

选一个已有同步能力的目录，例如 iCloud Drive 或 Syncthing 目录。不要把 `state/qmemory.sqlite3` 放进去。

设备 A：

```bash
.venv/bin/qmemory sync-push "/path/to/shared/qmemory-events"
```

设备 B：

```bash
.venv/bin/qmemory sync-pull "/path/to/shared/qmemory-events"
.venv/bin/qmemory sync-push "/path/to/shared/qmemory-events"
```

然后设备 A 再 `sync-pull`。建议在客户端启动和结束时分别 pull/push；自动文件夹同步只负责运输，QMemory 负责校验与合并。

同一 Git 仓库在不同设备路径不同也能归入同一项目：项目 ID 优先使用规范化的 Git remote；非 Git 目录才退化为绝对路径哈希。

## 接入 Codex

OpenAI 官方 Codex 文档说明，本地客户端支持 STDIO MCP，默认配置文件为 `~/.codex/config.toml`，桌面端、CLI 与 IDE 扩展在同一 Codex host 上共享配置。

最简单的方式是在 QMemory 首页点击“连接 Codex”。应用会先检查现有连接：路径失效或指向旧版本时，按钮会显示“修复连接”，经你确认后自动替换。

也可以在 Codex 设置 → MCP servers → Add server，选择 STDIO，然后填写：

```text
Name: qmemory
Command: /absolute/path/to/qmemory/.venv/bin/qmemory
Args: mcp
```

也可以用 CLI：

```bash
codex mcp add qmemory -- "$(pwd)/.venv/bin/qmemory" mcp
codex mcp list
```

Claude Code 也可连接同一个 QMemory MCP：

```bash
claude mcp add --scope user qmemory -- "$(pwd)/.venv/bin/qmemory" mcp
claude mcp get qmemory
```

两个 Agent 调用同一组工具、读写同一个判断库；会话原文则按 `codex` / `claude-code` 分源存放。

QMemory 暴露 15 个 MCP 工具：

```text
agent_conversations_sync  codex_conversations_sync
conversation_search       conversation_get
insight_pipeline_status
project_context       memory_search        memory_get
memory_propose        memory_confirm       memory_supersede
record_incident       session_handoff      memory_feedback
sync_status
```

建议客户端协议：任务开始先调用 `agent_conversations_sync`，再调用 `project_context`；需要核对历史证据时使用会话检索；新判断先 `memory_propose`；只有用户明确确认后才 `memory_confirm`；任务结束写 `session_handoff`。

“启用自动记忆”会把一段带版本标记的受管协议同时写入 `~/.codex/AGENTS.md` 和 `~/.claude/CLAUDE.md`。它只负责行为策略：何时归档、何时读、哪些内容能写、何时确认和如何交接；实际数据仍通过 MCP 写入 QMemory。已有全局指令会保留，后续升级只替换 QMemory 自己的受管区块。

## SuperLocalMemory 边界

QMemory 自带可工作的 SQLite FTS 召回，不依赖 SuperLocalMemory。`adapter-status` 会检测本机 `slm`：

```bash
.venv/bin/qmemory adapter-status
```

代码中提供了显式、单向的 `SuperLocalMemoryAdapter`，但 v0.6 不自动双写：QMemory 的哈希事件仍是唯一同步真相，SLM 只允许作为可重建的本机召回投影。这避免 SLM 重建、升级或多设备协调反向污染判断历史。

## 安全边界

- GitHub token、云 SecretId、私钥、password/API key 等模式会在写事件前脱敏；
- MCP 召回内容被声明为证据，不是可执行指令；
- 生命周期操作必须同项目，默认不跨项目召回；
- 当前只是规则脱敏，不应主动把任何凭据交给记忆系统；
- 原始 rollout 可能包含工具输出与凭据，QMemory 原样归档是为了保真；目录权限设为当前用户可读写，但当前还没有静态加密；
- 原始会话不会上传到 QMemory 的设备同步目录，洞察提取前会先规则脱敏；
- v0.6 未加事件加密。若判断同步目录会上云，使用有端到端加密的文件同步方案，或等待后续加密分片。

## 验证

```bash
cd /path/to/qmemory
.venv/bin/pytest -q
.venv/bin/qmemory --home /tmp/qmemory-smoke init --device-id smoke-device
.venv/bin/qmemory --home /tmp/qmemory-smoke doctor
```

当前测试覆盖判断生命周期、项目隔离、写前脱敏、双设备合并、旧副本、同设备分叉隔离、并发替代收敛，以及原始会话无损归档、增量更新、项目访问边界和洞察证据引用。

重建桌面应用：

```bash
cd /path/to/qmemory
scripts/build_macos_app.sh
```
