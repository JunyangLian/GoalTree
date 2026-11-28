# 05 · 原 Runner 复现环境（Docker 环境到达后的执行步骤）

状态：本机无 Docker、Python 3.10.1，且当前到 huggingface.co 的网络不稳定，故本文为**到达复现环境后的操作清单**，不是本机已执行结果。所有命令均来自官方 Runner README / 指南 / 源码（详见 `00_audit.md`），未改写与官方不同的评分器或协议。

## 1. 前置条件

- Linux（推荐 Debian 13 或 Ubuntu），**Docker** 可用。
- **Python ≥ 3.13**（官方 Runner `pyproject.toml` 要求）。
- 一个可用模型 API key（官方 Runner 走 Harbor 接入 Codex / Claude Code / OpenCode；用 GoalTree 接入时由本仓库 `pico.providers` 客户端承载出站调用，需显式记录网络边界差异）。
- 磁盘：官方数据规模**待 HF 网络恢复后再确认**（本文第 4 节列出全部文件，大小暂标 TBD）。

## 2. 安装官方 Runner

```bash
# 从 HF 下载官方 Runner 源码（或直接 git clone 该 dataset 仓库）
# 进入 omicsbench_runner/ 目录后：
pip install .
```

官方 README 明确：安装需 Python 3.13+ 与 Docker；Runner 依赖 Harbor + Daytona 沙箱（Docker）。

## 3. 配置与校验（官方命令）

```bash
# 生成 omicsbench.toml，并在 [paths] 设 questions_dir 与 grader_databases 指向官方文件
omicsbench setup

# 校验配置、grader 数据库、任务
omicsbench validate all

# 运行全部任务 / 单个任务
omicsbench run
omicsbench run --question <task JSON>
```

等价 Python API（官方公开）：
```python
from omicsbench.data import download_databases, download_questions
from omicsbench.run import run_benchmark, dry_run_benchmark
from omicsbench.validate import validate_everything
```

## 4. 数据与数据库清单（官方，大小待确认）

grader 数据库：`gideonshaked/OmicsBench-grader-databases/grader_databases.sqlite`（大小 TBD）。

`gideonshaked/OmicsBench` 的 `questions/`（3 项公开任务）与其 `data/`：

| 任务 | 数据文件 | 格式 | 大小 |
| --- | --- | --- | --- |
| bovine_imf_sf_preadipocyte_origin | gene_expression_profile.csv.gz | csv.gz | ~KB级（小） |
| melanoma_kd_migration_program | gene_expression_tpm.xlsx / sample_metadata.csv | xlsx/csv | 小 |
| adult_kidney_pt_vcam1_state | snatac_fragments.tsv.gz（+.tbi）/ snatac_gene_activity.h5ad / snrna_expression.h5ad | tsv.gz/h5ad | **较大（较大 h5ad）** |

> 关键：`adult_kidney` 的两个 `.h5ad` 与 `snatac_fragments.tsv.gz` 可能占 GB 级。真实运行时优先从最小的 `bovine`（csv.gz）开始做 1 节点端到端，再逐项扩大。下载前先确认总大小与磁盘配额。

## 5. goalTree 接入路径与差距

官方 Runner 以 Harbor `BaseAgent` 为 agent 集成点（`_orchestrator/harbor/agents/session_agent_base.py`）。让 GoalTree 复用官方 Runner 驱动逐轮 + 官方 grader，需实现一个 `BaseAgent`（Harbor）子类，内部调用 `Pico.start_run(execute_actions=False)`。**该 shim 尚未实现、不可测**（需 Python 3.13 + Harbor + Docker），未写为未验证代码。

本仓库已就绪、可与官方 Runner 对齐的部件：
- `benchmarks/omicsbench/`：官方 question schema 加载 + 官方提示渲染（防泄露）+ DAG 逐轮 + 答案结构分类（不评分）。
- `goaltree/scientific/`：约束抽取 / 证据校验 / 定向纠偏（离线全测通过）。
- `benchmarks/omicsbench/config.py`：A/B/C/D 冻结消融组。

## 6. 到达 Docker 环境后的最小执行顺序

1. 先跑 `omicsbench validate all` 确认官方 Runner 可自检通过。
2. 用**最小任务** `bovine` 跑官方 Codex/OpenCode 基线拿到可复现的官方评分口径（即便只 1 节点）。
3. 再以 GoalTree shim 替换 agent，跑 A（基线）→ B/C/D，保持同模型、同预算、同工具。
4. 每轮记录：模型/协议/温度/预算/Runner revision/GoalTree commit/OS/容器规格/墙钟/token/输出；缺分数记 `null` 不填零。