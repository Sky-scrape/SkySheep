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

## 现有场景（8 个）

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

## 写新场景

辅助函数在 `_harness.py`（场景文件 `from evals._harness import ...`）：

- `make_agent(provider, project_dir, *, gate=None)` — 组装真实注册表 + 真实
  权限门的 Agent（与 backend 装配同构）；调档位就自建 `PermissionGate`
  传入、构造后设 `auto_accept_write` 等属性（与产品改法一致）；
- `run_eval_turn(agent, text, decide=None)` — 跑一轮真实循环、收集事件流；
  `decide(request_event) -> "allow_once"|"allow_always"|"deny"` 按事件内容
  回决策，**不传一律 deny**（fail-closed：忘接决策的后果是「什么都没执行」）；
- `requests(events)` / `resolved(events)` / `finished(events)` — 事件流切片。

夹具只有 `home`（SKYSHEEP_HOME 隔离 + `proj/` 临时项目目录）。新场景文件
放进本目录即自动获得 `eval` 标记（conftest 的 `pytest_collection_modifyitems`
钩子），无需手写 `pytestmark`。走后端管线的场景（如场景 5）直接用
`ServerBackend`，事件是 dict——按 `e.get("kind")` 过滤，别套 agent 事件的
属性访问。

## 目录结构说明

evals 做成包（有 `__init__.py`）是刻意的：`tests/` 的测试文件按顶层名导入
`from conftest import FakeProvider`，两个平级目录各放一份 `conftest.py` 会
争抢 `conftest` 这个顶层模块名（先导入者赢，另一边全部 ImportError）。
包化后本目录 conftest 以 `evals.conftest` 导入，互不干扰。
