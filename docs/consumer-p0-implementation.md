# QMemory 使用方 P0 改进与验收

日期：2026-09-10。依据：`agent-consumer-review-2026-09-10.md`；保留评审原文。
P0 实现阶段仅进行本地源码实现及验证，当时未提交、推送、安装或发布。已安装的 MCP 进程及用户
全局 Agent 指令没有被改写；以下行为需要部署新版并重载客户端后才对它们生效。

## 已复现的原因

1. `context_pack` 原来直接调用 strict search。FTS 与 LIKE fallback 都将多词用 AND
   拼接；一个词不在记忆中就过滤掉该记录。已确认状态仍会消失。
2. 每次上下文召回还会追加每条命中的 impression 事件并重建整个投影。
3. `session_handoff` 找到当前交接就调用 supersede，不比较文本；查找还受 browse
   的 100 条上限及反馈排序影响，可能遗漏真正的当前交接。

先增加四组失败测试，再实现修复。调试过程中另观察到 SQLite 默认 FTS 对连续中文
词片段的精确搜索限制；本轮不改 `memory_search` 的旧语义，context 的补充匹配不再
依赖它。独立搜索的分词/语义召回优化仍属后续工作。

## 1. 项目基线 + 查询补充

- 同一项目内只选 `active`。保留既有 lifecycle 语义，不将 proposed、archived 或
  superseded 自动晋升成基线。
- decision、constraint、incident、status、handoff 每类先取一条最新 active 记录。
  status/handoff 不再补入同类较旧版本。其余稳定记录按查询词部分匹配和时间补充。
- 查询可排序基线，但不作为基线的过滤条件。limit >= 5 时保留五类的代表；limit < 5
  是调用方主动缩小容量，不能同时承诺五类覆盖。每类候选上限 3、相关候选 48，最终
  最多 12 条，避免在一次工具响应里倾倒整个项目。
- 每项含 `why_selected`、`status`、`source_ref`、`commit_sha`、`as_of` 和 `stale`。
  status/handoff 默认 3 天复核、incident 180 天、decision/constraint 365 天；缺失、
  无时区或未来时间标 stale。这里是确定性复核提示，不是自动失效/删除策略。
- 默认 statement 预览 160 字符、subject 60 字符。若整体过大先缩短预览，再减少末尾
  条目；超长 source_ref 不生成错误截断链接，返回 null + `source_ref_omitted`。
  通过 memory_id 再调用 memory_get 读取完整依据；本次没有裁剪存储中的原文。
- `revision` 是所选原始记忆内容、状态、引用等的摘要，不因一次曝光统计改变。
  它不是全项目账本版本，也不是跨设备通用 occurrence ID。
- JSON payload 估算：非 ASCII 字符按 1 token、ASCII 按 4 字符/token。context 装配
  预算约 1400，bootstrap 为同步元数据预留空间。不是特定模型 tokenizer，也不包含
  MCP transport 可能重复的 structured/text 序列化、系统提示与长上下文重读成本。

## 2. 条件式 project_bootstrap

工具参数：`project_path`、`query=''`、`limit=12`、`ttl_seconds=300`（0–86400）。
0 表示立即检查，不是强制重拷贝。显式 `agent_conversations_sync` 工具仍保留原有
全来源归档返回结构，并更新成功同步的新鲜度缓存。

```text
一次 bootstrap
  ├─ 同步锁忙 → 返回已有上下文 + busy / fail_open
  ├─ 成功检查仍在 TTL 内 → fresh，无目录扫描与复制
  └─ TTL 到期 / 首次调用
       ├─ 元数据指纹相同 → unchanged，无归档与复制
       └─ 指纹变化 / 索引丢失 → 现有归档器增量同步
                                └─ 无论成功/失败，再尝试装配已有上下文
```

指纹来自现有 Codex/Claude 来源发现范围内的路径、大小、mtime、ctime、inode 的组合
摘要，不读取消息或图片正文。摘要及源根目录的摘要键保存在本地 usage.sqlite3。
TTL 内新消息最多延迟到下一次检查；TTL 不是实时文件监听。到期检查仍需要 O(来源
文件数) 的 metadata 扫描。极大目录/网络文件系统的延迟尚无超时预算保证。

只在无失败的同步后更新 last_sync_at。到期无变化仅更新 checked_at；sync_age 是
距实际成功归档的秒数，而非距最近元数据核对。失败不得冒充已新鲜。没有复制任务
发生的 fresh/unchanged 计为同步 no-op；busy/失败不计成功 no-op。

同一新版服务使用非阻塞 bootstrap 文件锁，多个新客户端不会同时发起归档。
显式同步保留等待语义。旧版本桌面进程不认识这个锁，需统一升级后才能保证协作；
跨不同机器不由该本机锁协调。归档失败、目录错误和数据库错误只返回错误类型，不
泄露原始异常路径/密钥。进程完全不可用时仍靠宿主执行 fail-open 协议。

返回含 sync_performed、sync_reason、sync_age、last_sync_at、new_conversations、
updated_conversations、imported、files_scanned、bytes_copied、revision 和
estimated_payload_tokens。新增会话数与已有会话更新数明确分开。归档范围仍是既有
本机来源集合，不意味着这些来源已经提炼成记忆。

## 3. 差异式 handoff

- 对当前项目、type=handoff、subject=current work、holder=agent 的最新 active
  记录精确查询，不经过通用搜索的反馈排序或 100 条截断。
- NFC + 空白规范化 + 安全脱敏后的文本完全相同则返回原 memory_id、`noop=true`、
  `changed=false`，不追加/替代记忆事件，也不更新时间、来源或确认日期。
- 即便新的 source_ref 不同，相同内容仍 no-op；新证据不会因此被悄悄覆盖。若需要
  单独补充来源，应通过后续明确的 provenance 操作，不重复记忆。
- 使用项目级本地锁覆盖比较和写入，测试四个并发写入只产生一个 handoff。
- 新内容仍沿用既有 supersede 链和 handoff 的 active 状态语义；不改其他类型的人类
  确认要求。新增 1–4000 字符限制，不接受空日报或整份长报告。
- 本轮不宣称语义去重。不同措辞、标点、大小写不自动视作同一判断；规范化适用于
  简短自然语言交接，不适合把依赖缩进语义的代码当作交接全文。

## 4. Agent 协议 v4

协议源与 MCP instructions、README 已统一：简单任务零调用；同会话紧密开发且
上下文新鲜时不反复同步；跨日/跨 Agent/设备或上下文不足时一次 bootstrap；只在
状态/未完成项改变时写短 handoff。旧服务没有 bootstrap 时仅尝试一次 context。
错误提示一次后继续开发，不循环补搜和重试。

“常规最多两次往返”是宿主行为目标，不是服务端强制配额。新的正式判断仍然需要
propose、明确确认与必要的 supersede；不能为了省工具调用跳过审批。
自动编辑用户全局 AGENTS.md/CLAUDE.md、重启已安装 MCP 和打安装包不在本轮范围。

## 5. 使用成本和质量口径

新增 `usage_stats`，以及原 quality-report 的 `summary.usage`。独立的本地 operational
SQLite 保存累积计数及最近 1000 次时延/token 数值样本；不存 query、statement、
source_ref 或错误正文。不写进记忆事件链、不云同步。统计损坏/不可写时不阻断主流程。

- 全局 MCP 工具调用总数（tool:*）；项目内 context/bootstrap/search/handoff 操作数。
  MCP 包装器与 service 各计各自层，不能相加冒充模型往返次数。
- 空响应比例（含 unavailable 的空响应，错误数另列）、上下文返回条目数、handoff
  no-op 比例、bootstrap fresh/unchanged 比例、扫描/复制量、payload/时延 P95。
- `retrieval_stats` 同时列出旧账本曝光与新本地曝光；原 impressions 为两者之和。
  新曝光不会写入记忆账本或触发投影重建，但它是本地统计，不能跨设备完整恢复。
- 补搜意图率、模型往返数、token-equivalent 开销返回 null，需要宿主 instrumentation。
  helpful/ignored/rejected 继续依赖真实反馈事件；没有反馈就显示未知，不能填 100%。
- 元数据缓存/统计是可丢弃的运行数据。删除 usage.sqlite3 会丢统计并引起一次重新核对，
  不删除任何记忆或原始会话。没有自动清理用户已有库。

## 验收与报告

```sh
.venv/bin/pytest -q
PYTHONPATH=src .venv/bin/python scripts/consumer_p0_report.py --output artifacts/consumer-p0-report-2026-09-10.json
# 可选：只读既有 OpenDesign 索引，不输出原始记忆正文
PYTHONPATH=src .venv/bin/python scripts/consumer_p0_report.py --live-readonly --output artifacts/consumer-p0-report-2026-09-10.json
```

回归语料 `tests/fixtures/opendesign_consumer.json` 是合成数据，不含真实私人会话。
包含跨日续作、故障修复、纯 UI 微调和简单问答的协议样本；后两者零调用是协议目标
和模拟场景，不代表实际测到了所有宿主行为。

首次完整验证：129 tests passed，1 个依赖警告（Pydantic lifespan forward reference）。
OpenDesign 合成 20 问法 Top-5 基线集合命中 100%，空响应 0%；payload 估算中位数
709 / P95 711 token，context wall P95 27.04ms；只读召回新增事件 0，相同 handoff
新增事件 0，无变化归档复制 0。数字为本机该次运行样本，重跑以 JSON 报告为准。

真实本地只读对照（20 问法）：opendesign 旧严格搜索空 19 次、新 context 空 0 次；
opendesign-docs 旧严格搜索空 20 次、新 context 空 0 次；前者标出了 1 条 stale 记忆。
不输出正文、不重建或写入真实库。这个实验只证明基线可恢复，不能证明召回帮助率。

整体产品质量报告仍为 **not release ready**：合成归档样本没有完整提炼闭环，且没有
真实用户 helpfulness 反馈。未实现语义去重、语义 embedding 召回、来源 watcher、
off/compact/full 档位、真实 Agent 调用预算强制器、云共享或客户端安装升级。

下一步是用户授权升级已安装的 MCP 与 v4 受管协议后，由 OpenDesign 使用方在真实
开发中验收：跨日一次启动、连续编辑不反复同步、相同交接 no-op、断连继续工作，
并从宿主侧补齐模型往返与有效 token 开销。

## 后续 GitHub 同步范围

用户随后明确要求同步 GitHub。同步范围为本次源码、合成回归测试、评审及实现说明；
不包含私人会话、数据库、凭证和 artifacts 运行报告，不涉及安装、全局协议升级或云服务部署。
同步前复验：129 tests passed，1 个上述依赖警告；compileall 与 git diff --check 通过。
合成报告 P0 门槛全部通过；本次 context wall P95 为 40.52ms，payload P95 为 711
估算 token。GitHub 同步结果以实际提交和远端分支为准，不代表产品完整闭环已验收。
