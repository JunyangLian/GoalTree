# 03 · 实验计划（冻结配置，不启动批量试验）

## 消融矩阵（同一底层模型 / 同一任务 / 同一工具集）

| 组 | 配置 | 用途 |
| --- | --- | --- |
| A | GoalTree 原版 + 官方 Adapter | 基线 |
| B | A + Constraint Compiler（仅提示/记录，不拦截） | 约束抽取独立影响 |
| C | B + Evidence Verifier | 验证机制增量作用 |
| D | C + Targeted Recovery | 定向纠偏增量作用 |

`benchmarks/omicsbench/config.py::ABLATION_GROUPS` 冻结三布尔标志；`RunSpec` 记录模型/协议/温度/gated/预算/环境，实际值必须来自真实调用，不得猜测。

## 数据划分

- 公开 3 项 OmicsBench：仅 Runner 接入、开发验证、错误分析，全部标记 development/public。
- 自行构建/专家审核任务：冻结问题、答案、约束、评分标准，记录来源与许可。
- 私有集：如作者允许评测，遵守其协议，不回流答案到开发。
- 禁止以公开答案/基因名单/预期分为中心手调提示/代码再同题宣称泛化。

## 指标

主指标：官方 OmicsBench 平均任务分（仅符合官方评测条件时；否则标“本地开发分”）。
机制指标：criteria_violation_rate、scope_violation_rate、unsupported_result_rate、
false_intervention_rate、recovery_success_rate、tool_calls / input_tokens / output_tokens /
wall_time / estimated_cost / timeout_count。

## 防 Benchmark Gaming

- 提交路径只可访问题目、数据、合法前序结果。
- Grader/answer key 隔离在 Agent 容器与 Tool Registry 之外；目录权限 + 回归测试双重保证。
- 单轮正式提交前不得多次请求 grader 分数；gated 只接受官方协议反馈。
- 可疑“猜答案/放宽阈值直到及格”单独标注，不计为科学纠错能力。

## 推荐的下一次最小实验

- 任务：1 项公开任务（先 `bovine_imf_sf_preadipocyte_origin`）。
- 对照组：A/B/C/D 四组，同模型同预算同工具。
- 指标：criteria_violation_rate + false_intervention_rate + 官方分。
- 预计资源：1 台带 Docker 的 Linux（Python ≥ 3.13）+ 一个已授权模型 key + 官方数据下载（约数十 GB，需评估）。
- 授权：需要用户确认模型额度与数据下载（h5ad/tsv.gz/xlsx，可能较大），并确认不触碰私有集。