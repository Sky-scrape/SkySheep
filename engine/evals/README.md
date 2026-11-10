# engine/evals — 行为级评测回归基线

用 **fake/scripted provider 驱动真实 Agent 循环**（真实工具注册表、真实权限门、
SKYSHEEP_HOME 临时隔离），断言**事件流与工具调用序列**的回归场景集。
与 `tests/` 的单测互补：单测钉函数与协议，评测钉「组装起来的行为」——
每个场景对应一条项目真实回归史，产品行为一旦变化，这里先红。

## 与单测的边界（写场景前必读）

- **不评文本质量**：不比对模型输出措辞，只看事件序列、工具调用与文件系统后果；
- **不联网**：provider 一律 `FakeProvider` 脚本化回放；场景里确需真实执行的
  命令必须选无副作用、不触网、立即返回的形态（如 `git status`、裸 `docker run`
  ——缺参数在 CLI 端秒退），危险命令一律 deny、不执行；
- **不为好测改产品**：评测基建不得为了让场景好写而改引擎行为。发现行为与
  预期不符，先怀疑场景、再怀疑回归，都不是就上报，不在 evals 里绕；
- **隔离**：`SKYSHEEP_HOME` 由 `conftest.py` 的 `home` 夹具指向临时目录，
  任何场景不得指向真实 `~/.skysheep`。

## 跑法

```bash
uv run pytest -m eval                        # 全部评测基线
uv run pytest evals/test_eval_whitelist_scenarios.py -m eval   # 单个场景文件
uv run pytest                                # 默认集（不含 evals，行为与从前一致）
uv run pytest -m "not e2e and not eval"      # 跳过 e2e 与评测基线
```

**为什么单跑文件也要带 `-m eval`**：pyproject 用 `testpaths = ["tests", "evals"]`
+ `addopts = '-m "not eval"'` 实现默认排除——不带路径的 `-m` 会覆盖 addopts 里的
`-m`（argparse 后者胜），所以 `uv run pytest -m eval` 原样可用；而**显式给路径**
（如 `pytest evals/xxx.py`）时 addopts 仍生效、eval 项会被剔除，必须补 `-m eval`。

**默认集为什么不混入 evals**：备选方案是「evals 不进 testpaths」，但那样
`pytest -m eval`（不带路径）只收集 testpaths 里的 tests/，一条评测都跑不到。
选 testpaths + addopts：默认 `uv run pytest` 与 `--collect-only` 的用例集、
数量与改动前完全一致（evals 在收集期即被剔除，不拖慢默认运行）。

**已知交互**：CI 的 `-m "not e2e"` 是显式 -m，会覆盖 addopts、连评测一起跑
（评测不联网、不碰真实 home、进程内秒级，跑上只增加回归保护）；`-m e2e`
层不受影响。

## 现有场景（34 个）

| # | 场景 | 文件 | 回归史出处 |
|---|---|---|---|
| 1 | 只读并发批夹带 memory_write → 必弹确认、全局记忆不落盘 | test_eval_gate_scenarios.py | 安全审查「并发只读批绕门」 |
| 2 | 删除目录必经确认：delete_file 与 run_command 删除命令都拦得住 | test_eval_gate_scenarios.py | 「别教模型用 run_command 的 rm/mv」+ DANGEROUS 强制确认 |
| 3 | move_file overwrite 覆盖已有目录 → 「自动允许写入」档下仍逐次确认（删除形态守卫），预告规则是 exact | test_eval_gate_scenarios.py | 安全审查 A-1/A-2 |
| 4 | docker run「总是允许」只沉淀 exact 不沉淀两词前缀，`docker run -v` 挂载仍要确认；存量前缀规则 fail-closed | test_eval_whitelist_scenarios.py | 安全审查项 12 |
| 5 | 未决权限被停止 → 事件流出现 decision=cancelled 的 permission_resolved | test_eval_stop_and_taskbook.py | 2026-10 审查项 5 场景 B |
| 6 | 任务簿终态超限时 list_tasks 保留全部 running/queued | test_eval_stop_and_taskbook.py | 任务面板「活动任务消失」回归 |
| 7 | 前缀规则不覆盖 shell 拼接命令，确认弹窗带解释 | test_eval_whitelist_scenarios.py | `git status; rm -rf /` 拼接判定回归 |
| 8 | 「自动允许写入」档不放开工作目录外写入 | test_eval_gate_scenarios.py | 自动允许写入档的目录边界 |
| 9 | 流水线 DAG 顺序与失败传播：依赖未完成不派跑、上游产出注入下游、依赖失败级联（下游 runs=0）、收尾 failed | test_eval_pipeline_scenarios.py | 无人值守闭环二期「编排语义」回归 |
| 10 | 节点推送开关（关）：成功/依赖级联一字不发，节点自身执行失败必推（mock 渠道） | test_eval_pipeline_scenarios.py | 无人值守闭环二期「节点推送开关」 |
| 11 | 节点推送开关（开）：逐节点推送 + 与收尾推送去重，逐 chat 投递（mock 渠道） | test_eval_pipeline_scenarios.py | 无人值守闭环二期「节点推送开关」 |
| 12 | 渠道门无人值守写拒绝：approve 关立即拒绝不挂起不落盘；预授权名单照常放行（对照） | test_eval_channel_gate_scenarios.py | 渠道门「无人值守不卡死」+ 预授权名单语义 |
| 13 | 渠道审批卡路径：推卡挂起 → 决定只认发起人 → 「总是允许」降级单次、不沉淀规则 | test_eval_channel_gate_scenarios.py | 审查 S-08/P3-17（发起人校验）+ 渠道端 allow always 降级 |
| 14 | 渠道审批超时自动拒绝：不挂死、不落盘、待决项清空 | test_eval_channel_gate_scenarios.py | ChannelGate「超时兜底是硬要求」 |
| 15 | 渠道审批卡推送失败退化为拒绝：通道断了不能挂住循环更不能放行 | test_eval_channel_gate_scenarios.py | ChannelGate「推卡片失败要退化成拒绝」 |
| 16 | 检查点回滚还原文件：轮末自动落检查点；快照后被改过先报 conflict，force 才恢复（新建文件撤销=删除） | test_eval_checkpoint_scenario.py | 「每轮改动可撤销」承诺 + 并行写入冲突脏检查 |
| 17 | 上下文压缩边界（低于阈值侧）：占用 ≤ 上限×0.9 不压缩，第一次 provider 调用就是主调用 | test_eval_compaction_scenario.py | 「中文会话占用被低估、自动压缩触发过晚」回归 |
| 18 | 上下文压缩（超阈值侧）：超线先压缩——compaction 事件、摘要调用带分段标记、历史替换为 SUMMARY 标记消息+最近 N 条 | test_eval_compaction_scenario.py | 同上 + compaction 结构契约 |
| 19 | 记忆检索阈值切换（小记忆侧）：低于阈值整块注入，带不带查询逐字节一致 | test_eval_memory_retrieval_scenario.py | 记忆检索化第一期 |
| 20 | 记忆检索阈值切换（大记忆侧）：超阈值按相关性注入子集并附说明；零命中/无查询退回整块 | test_eval_memory_retrieval_scenario.py | 记忆检索化第一期 |
| 21 | 记忆写读闭环：真实 memory_write（经确认）落盘的条目进入下一轮注入 | test_eval_memory_retrieval_scenario.py | FINDING 2（memory_write 逐次确认）+ 「记住的下轮可见」 |
| 22 | web_fetch「框起来+标记」两态：边界框与免责声明恒在；命中话术尾部附提示、正文照常交付（只标记不拦截） | test_eval_web_injection_scenario.py | 注入纵深防御第一期（本机回环桩 mock 传输） |
| 23 | web_fetch 出厂默认装配拒绝非公网地址：回环/云 metadata/localhost 在解析校验层拒绝（SSRF 底线） | test_eval_web_injection_scenario.py | web_fetch SSRF 防护「不许放松」底线 |
| 24 | headless run 审计输出：结构化结果齐备；会话「▶ 指令」标题落库，user/assistant/tool 消息完整可回放 | test_eval_headless_scenario.py | `skysheep run`「消息落库可审计」 |
| 25 | headless run 无人值守写拒绝：HeadlessGate 未预授权写入不落盘、不挂死，写入尝试落库可审计 | test_eval_headless_scenario.py | HeadlessGate fail-closed + 审计 |
| 26 | 白名单 exact 边界：拼接命令沉淀的 exact 只放行当条——原样免确认，扩展与更短前缀命令都要重新确认 | test_eval_whitelist_matrix_scenario.py | 场景 4/7 的边界补充（exact 非 prefix） |
| 27 | 白名单 prefix 边界矩阵：完整词边界（statusx）、解释器旗标（-c）、拼接拦截解释、空 pattern fail-closed、停用规则单独提示 | test_eval_whitelist_matrix_scenario.py | 审查 B-1（代码旗标）+ explain 测试器契约 |
| 28 | 聊天渠道出站推送开关：定时任务 notify_channel 关=一字不发；开=推名单内每个 chat，空名单拒发；推送不影响任务行回写（mock 渠道） | test_eval_channel_push_scenario.py | 无人值守闭环第一期「定时任务终态推送」 |
| 29 | 隔离区模式开关两态：关=正文整体进上下文（一期行为）；开=摘录+隔离文件路径进上下文、全文原子落盘 quarantine/、边界行带信任级 | test_eval_web_injection_scenario.py | 注入纵深防御第二期（QUARANTINE_ENABLED，monkeypatch 切换） |
| 30 | Webhook 出站：enabled 门（关=configured 也不发）；开=整段 POST 一次、JSON 形状固定、secret 出可验签的 HMAC 签名头；不影响任务行回写 | test_eval_webhook_scenario.py | 通用 Webhook 渠道（真实 WebhookChannel + MockTransport） |
| 31 | 圆桌融合：主席也出草稿 + 两成员独立作答 + 主席融合——草稿不进主历史、融合答案带 roundtable 元数据落库、融合输入含全部草稿 | tests/test_collab_scenarios.py（本体）+ evals/test_eval_collab_scenarios.py（eval 挂载） | CHANGELOG [2.6.3] P29「协作行为 eval 基线另批」 |
| 32 | 团队闭环：AI 总管自动闭环（拆解 → 派工 → 验收 → 交付）——《交付说明》随 team_finished 落库、user+assistant 成对可审计、用量按角色入账 | 同上（本体 + eval 挂载） | 同上 |
| 33 | 对抗裁决：发现 → 调查 → 建议 → 裁判四阶段——裁决统计随 adversarial_finished 下发、终审报告带对抗元数据落库、用量按角色入账 | 同上（本体 + eval 挂载） | 同上 |
| 34 | 轮指标预算：只读工具的自动放行轮（工具 1 次、确认 0 次）与被拒绝的写入轮（确认 1 次、拒绝 1 次、零落盘）——`TurnMetrics` 逐项断言并附预算上限 | test_eval_metrics_scenario.py | 评测指标化（2026-11） |

## 指标层：除了过/不过，还记录多贵多慢

每条用例的结果与耗时由 `evals/conftest.py` 的插件自动记录，会话结束写
`evals/.metrics/last-run.jsonl`（逐条）与 `last-run.json`（聚合），并打印一行
`[eval-metrics] … 通过率 p50 p95`。开关：`SKYSHEEP_EVAL_METRICS=0` 关闭，
`SKYSHEEP_EVAL_METRICS_DIR` 改输出目录；`.metrics/` 是产物，不提交。

场景想要 token / 工具次数 / 确认次数 / 重试这些明细，用 `metrics` 夹具：

```python
from evals._metrics import TurnMetrics, assert_metrics

async def test_xxx(home, metrics):
    events = await run_eval_turn(agent, "看一下目录")
    m = TurnMetrics.from_events(events)      # 事件流 → 客观指标
    assert_metrics(m, max_tool_calls=1, max_permission_requests=0,
                   max_total_tokens=100, max_duration_ms=15_000)
    metrics.append(m)                        # 随结果落盘
```

预算只设上限、写宽一点（CI 慢机）：抓的是数量级退化（多调了十次工具、多弹了
三次确认、token 翻倍），不是毫秒级抖动。行为断言（「至少调一次工具」）仍写在
场景里，不混进预算。

报表：`python tools/eval_report.py`（Markdown，含 p50/p95、token 合计与逐场景
分桶）；`--json` 出机器可读；`--fail-on-failed` 供 CI 门禁用。

### LLM-as-judge（可选，不进默认回归）

文本质量归 `evals/judge.py`：`judge_answer(provider, question=..., answer=...,
criteria=...)` 让裁判模型只回一个 JSON（score 0–10 / verdict / reasons），解析
宽容（代码围栏、前后废话、字符串分数都认，解析失败记 `parsed=False` 而不是抛
异常）。它需要真实 Key、有随机性与费用，所以**不放进默认套件**——本机或发版前
手动跑，或者在自己的脚本里显式调用。`tests/test_eval_metrics.py` 用 FakeProvider
钉住解析与提示词契约，不产生真实调用。

## 写新场景

辅助函数在 `_harness.py`（场景文件 `from evals._harness import ...`）：

- `make_agent(provider, project_dir, *, gate=None, tools=None, **agent_kw)` —
  组装真实注册表 + 真实权限门的 Agent（与 backend 装配同构）；调档位就自建
  `PermissionGate` 传入、构造后设 `auto_accept_write` 等属性（与产品改法
  一致）；`tools` 替换注册表清单（如换装 web_fetch 的本机桩测试形态）；
  `agent_kw` 透传 Agent 构造参数（如 `context_limit_tokens`，供压缩边界
  场景调参）；
- `run_eval_turn(agent, text, decide=None)` — 跑一轮真实循环、收集事件流；
  `decide(request_event) -> "allow_once"|"allow_always"|"deny"` 按事件内容
  回决策，**不传一律 deny**（fail-closed：忘接决策的后果是「什么都没执行」）；
- `requests(events)` / `resolved(events)` / `finished(events)` — 事件流切片；
- `metrics` 夹具 + `TurnMetrics.from_events(events)` / `assert_metrics(...)` —
  轮指标与预算断言（见上「指标层」）。

夹具只有 `home`（SKYSHEEP_HOME 隔离 + `proj/` 临时项目目录）。新场景文件
放进本目录即自动获得 `eval` 标记（conftest 的 `pytest_collection_modifyitems`
钩子），无需手写 `pytestmark`。走后端管线的场景（如场景 5、9-11、16、28、30）
直接用 `ServerBackend`，事件是 dict——按 `e.get("kind")` 过滤，别套 agent
事件的属性访问；驱动后端前先把隔离 home 的 `ui.json` 预置
`{"update_check": 0, "notify": 0}`（评测不联网、不打扰真实桌面），管道场景
文件里的 `_make_backend` 是现成做法。

**协作场景（31–33）的本体在 tests/test_collab_scenarios.py**：场景函数在这里
实现并在默认套件跑（协作组装行为进默认回归），evals/test_eval_collab_scenarios.py
只做 re-export 挂载（本目录 conftest 自动打 eval 标记）——两处跑同一份实现，没有
两套断言。不这么放的话，直接 `pytest evals/xxx.py` 会因 addopts 一条都收不到
（见上「为什么单跑文件也要带 -m eval」），协作回归就只剩 `-m eval` 一条入口。

渠道/流水线出站推送的断言用 **mock 渠道**（`_MockChannel`，最小 duck-type：
enabled/configured/allowed_ids/send_text），塞进真实 ChannelManager 的注册表
（`be.channels.channels["feishu"] = ch`），不连任何真实聊天平台。两处既有
行为边界，写推送断言前先知道：①「after」是同批次序号，下游节点必须列在
依赖之后（前向引用会被宽松丢弃，DAG 静默变平）；②收尾汇总推送在两条补扫
并发时可能重复触发（状态守卫读的是本轮快照），节点行开关语义不受影响，
收尾唯一性由 tests/test_pipeline.py 在串行时序下钉住。

## 目录结构说明

evals 做成包（有 `__init__.py`）是刻意的：`tests/` 的测试文件按顶层名导入
`from conftest import FakeProvider`，两个平级目录各放一份 `conftest.py` 会
争抢 `conftest` 这个顶层模块名（先导入者赢，另一边全部 ImportError）。
包化后本目录 conftest 以 `evals.conftest` 导入，互不干扰。
