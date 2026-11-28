# OmicsBench 适配审计（00）

日期：2026-11-23
仓库：`d:\1_yanjiusheng\实习项目\agent\CodeTrail`（包名 `goaltree-agent`，origin `github.com/JunyangLian/GoalTree.git`）
本文只记录经本地源码/官方下载源码核实的结论。未核实处显式标注。

## 1. 仓库状态

- HEAD：`de9caad9d834e0e1b023e90e1e3cf09a42ae01dc`
- 最近提交（新→旧）：`Add injectable simulated clock for environments`、`Add resumable agent runs and external observations`、`Separate run outcomes and paginate tool outputs`、`Add shared environment and tool schema contracts`、`Improve GoalTree task contracts and audited workflow validation`
- 未提交修改：仅 1 个未跟踪文件 `assets/screenshots/goaltree-task-tree-20261108.png`（用户文件，本次不动、不覆盖）。
- Python：3.10.1；无 Docker、无 Podman（`docker`/`podman` 均 CommandNotFound）。
- 依赖：`pyproject.toml` 运行依赖为空；dev 仅 `pytest`、`ruff`。官方 Runner 需要的 `jinja2`/`attrs`/`harbor`/`daytona` 本仓库未声明。

## 2. 已有能力与拟复用点

| 能力 | 位置 | 复用方式 |
| --- | --- | --- |
| `Pico.start_run(msg, execute_actions=False)` | `pico/runtime.py:688` | 同进程逐步驱动，逐轮可见性入口 |
| `AgentRun.step(observation=None)` / `inject_event()` / `close()` | `pico/stepwise.py:33/67/103` | 逐轮 Action/Observation；投注环境事件 |
| step 语义（`action/observation/retry/final/closed`） | `pico/stepwise.py`、`docs/execution-environments.md` | 外部动作必须回传 Observation 才继续 |
| `EnvironmentProfile(name, role, instructions, budget_instruction, clock)` | `pico/environment.py:18` | 为 OmicsBench 提供宿主角色/任务指令 |
| `task_outcome`（completed/unknown/incomplete/failed）与 `resource_exhausted` | `pico/task_state.py`、`docs/execution-environments.md:130` | 区分“协议收尾”与“任务完成”，不回填分数 |
| 工具注册/审批/脱敏/预览/分页 | `pico/tools.py`、`tool_executor.py`、`runtime.py`、`output_store.py` | Observation 必须来自真实工具执行；复用审批与脱敏 |
| `RunStore` trace/report 持久化 | `pico/run_store.py` | 保存可审计轨迹 |
| `FakeModelClient(outputs)` 离线脚本模型 | `pico/providers/clients.py`、`pico/__init__.py` | mock 测试，不计分数 |

关键确认：`Pico.start_run(..., execute_actions=False)` 是**同进程**逐步执行，不是跨进程生成器持久化恢复（`docs/execution-environments.md:128`）。不据此宣称崩溃后续跑。逐轮 Action/Observation 契约已存在，OmicsBench 适配器应**复用而非重造循环**。

## 3. 官方 OmicsBench Runner 协议（已下载核对）

源：`https://huggingface.co/datasets/gideonshaked/OmicsBench`（license `mit-and-cc-by-4.0`），grader 数据库 `.../OmicsBench-grader-databases`（`grader_databases.sqlite`）。上游源码已下载至 `tmp/omicsbench_upstream/`（工作区外，不入库）。

### 3.1 关键事实
- 安装要求：**Python ≥ 3.13、Docker、模型 API key**（README）。
- 公开 3 个完整任务 `questions/*.json` + `data/`；其余为私有集。公开任务仅用于适配器开发验证，不得宣称为可泛化成绩。
- 每个任务 = DAG 节点（`Question.nodes[]`），按拓扑序逐轮呈现；`depends_on` 决定依赖。Agent 不应提前看到后续节点。
- 每节点 `grading.output_filename` + `graders[]`（`type`、`parameters`、`ground_truth`、可选 `output_key`）；`aggregation` ∈ {mean, all_or_nothing}。
- 答案 = 写在 `/workspace/answers/<output_filename>` 的严格 JSON，顶层 dict 键 = 每个 grader 的 `resolved_output_key()`。
- 结果 schema（`attempt_schema.py`、`run_metadata_schema.py`）：`Attempt.outcome` ∈ {scored, file_missing, unreadable, invalid_json, schema_mismatch, grader_exception, timeout, connection_lost}；仅 `scored` 有分数。
- 任务聚合分 = 各节点 `best_score` 的均值（`_aggregate_score`，`benchmark_runner.py:719`）。

### 3.2 逐轮提示契约（`prompt/`）
- `system_prompt.j2` + `data_context_prompt.j2` 只在第 0 个节点拼到 `node_task_prompt.j2` 前；后续节点是同一会话的后续轮（`benchmark_runner.py:530`）。
- `output_schema_deriver.py` 从 ground_truth 的**形状**生成 `<integer>/<string>…` 占位符；`choice_options` 来自 `parameters.choices`；`group_label_options` / `labeled_numeric_matrix_options` 只暴露组标签/行列标签。**不泄露 ground_truth 的数值/正确选项/条目**。适配器必须忠实沿用，不得手写“看似相同”的渲染（防泄露）。

### 3.3 Agent 与环境的耦合
- Runner 依赖 `harbor` 的 `BaseAgent`/`BaseEnvironment` 与 `daytona`（Docker）。`AgentSession.send_prompt` 调 `agent.run(prompt, env, ctx)`；`get_agent(...)`、`create_environment(...)` 与容器强耦合。
- grader 在 Agent 执行路径外运行（`grading_loop._download_and_grade_answer` 从容器下载答案后评分）。

## 4. 当前环境能否运行官方 Runner

**不能原地运行。** 三点硬阻塞：Python 3.10 < 3.13；无 Docker/Podman；`harbor`/`daytona` 依赖与远程沙箱凭证未安装。

因此本次 P0 交付为：**忠实复刻官方逐轮驱动 + 渲染 + 提交 + 外部评分的薄适配层**，用 `MockEnvironment` + `FakeModelClient` 做离线结束到结束验证；把“官方 Runner 原件”作为协议对照与后续 Docker/Python3.13 环境下的目标。**不声称 Runner 已打通、不报 mock 分数为真实成绩。**

## 5. 缺失接口与兼容性风险

| 缺失/风险 | 说明 | 处置 |
| --- | --- | --- |
| Docker 沙箱（数据只读 `/data/`、`/workspace` 可写、任务无网络） | 官方环境在容器实现 | mock 环境可运行；真实环境待 Docker 环境，标注“非论文同条件” |
| `harbor.BaseAgent` 接口 | Runner 以 Harbor agent 为集成点 | 适配器以 GoalTree `start_run` 替代 agent，需在将来环境提供 `harbor` shim，不改 Runner 协议 |
| Python 3.13 语法/特性 | Runner 本体 | 本仓库用 3.10 复刻驱动，不 import 官方包 |
| 官方 grader 依赖 `graders/*`、`grading`、sqlite | 评分语义与 synonym 数据库 | 已读源码；本轮不做等价重写，提交结果由外部 scorer 打分，缺分数不填零 |
| 逐轮可见性/防泄露 | 后续节点、ground_truth、grader 隐藏标签 | 只渲染当前节点 + 官方允许的 shape/choice/labels；回归测试断言未来节点与 ground_truth 值不在提示中 |

## 6. 最小文件修改清单（本次）

新增（不改现有 Agent 核心）：
- `docs/omicsbench/00_audit.md`、`01_adapter.md`、`02_constraints.md`、`03_experiment_plan.md`
- `benchmarks/omicsbench/`：`__init__.py`、`question.py`、`prompt_renderer.py`、`env.py`、`adapter.py`、`config.py`、`run.py`、`summarize.py`、`README.md`
- `goaltree/scientific/`：`__init__.py`、`contracts.py`、`compiler.py`、`evidence.py`、`verifier.py`、`recovery.py`
- `tests/test_omicsbench_*.py`（适配器/渲染/约束/验证/纠偏，全部离线）

不改动：`pico/**`、`goaltree/cli.py` 等既有入口；新能力默认关闭，仅显式 `--harness omicsbench` / 引入 `goaltree.scientific` 才生效。

## 7. 不能确定的事实与验证办法

- 官方 Runner 的 `harbor`/`daytona` 精确版本与沙箱网络边界：源码 `uv.lock` 未下载，需在具备 Docker 的环境安装后核对。
- 官方 grader 各 preset 的具体等价/距离函数边界（`graders/*` 未逐文件读）：评分由外部 scorer 执行，本轮不重写；后续需补读 `_grading/graders/**` 与 `subscore_aggregation.py`。
- 模型调用协议（Harbor 内如何把 token/cost 回读到 `AgentContext`）：见 `session_agent_base.py`（27135B），本轮未逐行核对成本字段名，适配器 mock 阶段以 `cost_usd=None` 表示未知。