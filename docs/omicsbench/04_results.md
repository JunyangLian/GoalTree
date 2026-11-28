# 04 · 真实模型验证结果（development / public，非论文同条件，无官方分）

日期：2026-11-24
范围：真实模型（GLM 5.3-Flash）+ 真实数据（`bovine` 公开任务 node1）+ 真实本地 shell（Windows host，无 Docker）。**非官方 Runner/容器条件，不构成 benchmark 分数。**

## 1. 环境与配置

| 项 | 值 |
| --- | --- |
| 模型 | `glm-5.3-flash`（智谱，`open.bigmodel.cn/api/paas/v4`，chat_completions） |
| 协议 | GoalTree 文本工具协议（`wire_protocol=text`） |
| temperature | 0.2 或 1.0（如实记录） |
| reasoning_effort | low（glm-5.3-flash 为 always-thinking） |
| 执行环境 | `HostEnvironment`：真实 subprocess 跑命令，`/data`→`D:/1Bio_Soft/omicsbench/data`、`/workspace`→`D:/1Bio_Soft/omicsbench/work` |
| 数据 | `gene_expression_profile.csv.gz`（18872×19，含 IMF1-4/SF1-4 原始 counts 与预计算 DE 列） |
| 任务/节点 | `bovine_imf_sf_preadipocyte_origin` / `preadipocyte_marker_divergence` |
| GoalTree commit | `de9caad9d834e0e1b023e90e1e3cf09a42ae01dc` |

## 2. 端到端能力（已验证）

- 真实模型可被 GoalTree `start_run(execute_actions=False)` + 逐步 Action/Observation 正确驱动。
- 真实 shell 执行：模型用 `python` + pandas/scipy 读真实 csv、跑 t 检验，Observation 来自真实 stdout。
- 参考答案（我按题目 `log2(raw+1)` + Welch 不校正 p<0.05 自算）与官方 ground truth **完全一致**：`higher in IMF = [PDGFRA, CD34]`、`higher in SF = [PPARG]`。
- **早退机制**：答案文件一旦 schema 合法即停止，同轮正确跑时 `write_file` 从 7~14 次降到 **1** 次。
- **定向恢复**：答案缺失/非法时回注入纠偏提示并重试（有上限，缺证据不伪造完成）。
- **科学验证管线（P1+P2+P3）**：约束编译 + 证据校验 + 定向纠偏已在 driver 接入，无误报。

## 3. 关键发现：模型方法遵循度是随机的（核心实证）

同一模型、同一任务、同一数据，多次运行出现**两种截然不同的行为**，正是本研究要抓的「方法漂移」的真实例证：

| 运行 | 方法 | 答案 | 验证器判定 |
| --- | --- | --- | --- |
| 若干次（temp=1 / temp=0.2） | ✅ `log2(raw+1)`+Welch 不校正 | ✅ 与 ground truth 一致 | 无 VIOLATION |
| 一次（temp=0.2） | ❌ 用预计算 `log2FoldChange`/`pvalue`/`category` 列 + 错误 schema | 错误 | schema 不符 → timeout |
| 一次（temp=0.2，--scientific） | ❌ 用了校正/调整后的 p | 未产出合法文件 | **C001 VIOLATION** + answer_provenance VIOLATION |

- 漂移被 P2-V1 在真实执行轨迹上**当场判定为 VIOLATION**，而非事后人工发现。
- 这类漂移用「温度」无法消除，属模型行为随机性，佐证「显式科学约束 + 证据校验」的必要性。

## 4. 验证器的诚实的边界（无过度拦截）

- p 阈值约束：方法无漂移 + 有答案文件 → `PASS`；否则 `UNVERIFIED`。精确 cutoff 未从数据重算，故 PASS 的理由里明示「未独立重算 cutoff」。
- direction（up/down）：需逐基因 fold-change 证据，名单/命令文本不可判 → `UNVERIFIED`。
- data_scope：需专用样本使用清单，缺 → `UNVERIFIED`。
- 无答案文件 → `answer_provenance VIOLATION`（空/缺文件不算通过）。

## 5. 已知局限 / 待办

- 未重算筛选阈值：p<0.05 等「数值阈值是否真被遵守」需要接结果表重算（`verification_strategy=table_recompute` 尚未实现）。
- direction/scope 仍为 UNVERIFIED：需要接「逐基因结果表 / 样本清单」证据源。
- Windows 开发 shim 引入 Linux/Windows 路径与命令摩擦；真实 Docker 容器可消除。
- 未运行官方 grader；`summary` 中缺分数记为 `null`，不填零。
- 无 Docker / Python3.13，官方 Runner 原件未原地跑（见 `05_reproduction_env.md`）。

## 6. 文件清单（本阶段）

- `benchmarks/omicsbench/`：`question.py`、`prompt_renderer.py`、`env.py`（Mock/Host）、`adapter.py`（early-exit + recovery + scientific）、`smoke.py`（--scientific/--env host）、`config.py`、`run.py`、`summarize.py`。
- `goaltree/scientific/`：`contracts.py`、`compiler.py`、`evidence.py`、`verifier.py`（V1/V2/V3 + answer 证据源）、`recovery.py`。
- 测试：`tests/test_omicsbench_adapter.py`、`tests/test_omicsbench_scientific.py`（34 项离线，全绿）。