# 03 · 演进路线（P2–P5）：Teacher、Skill、QLoRA、GRPO、路由

本文件只定义方向、接口与验收门槛，**不含已完成的训练结果**。P0/P1 的实际执行证据见
`01_small_model_baseline.md`、`02_harness_optimization.md`、`04_execution_report.md`。

## 0. 参考项目结论（MERA-Evolve，README 级审查）

来源：<https://github.com/yh-yao/MERA-Evolve>（README，2026-11 读取）。

- 目标：用 **verifier-grounded traces** 循环提升小模型；每轮从失败调用中挖掘
  “执行验证过的 teacher 示范”，更新 SkillBook，训练 student LoRA（SFT，可选 GRPO），
  再把学生模型放到**成本校准的路由器 + verifier 兜底**后面。
- 报告的主结果（论文口径，**不是本仓库结果**）：Qwen2.5-Coder-1.5B 在 HumanEval+MBPP
  582 个留出任务上 direct pass 28.7% → 49.7%，部署后 88.3%，成本为 always-large 的 60.8%；
  TAU-2 上 Qwen3.5-2B GRPO 14/35 → 18/35。
- 前置条件：Linux、Bash、NVIDIA GPU、vLLM、verl；训练与推理分离。
- 可借鉴的思想（不照搬架构）：失败→教师示范→**重新执行验证**→入经验池→Skill/SFT/GRPO→
  独立评测→通过才发布；路由器只在有 verifier 兜底时使用。

## 1. P2：Teacher Guidance + Skill 自学习

### 1.1 失败分类（基于现有 trace）

输入只用当前 run 的：任务指令、适用约束、已执行工具与返回、验证器结果、已尝试恢复。
分类枚举（固定字符串，便于统计）：

```
planning | tool_selection | tool_arguments | context_missing |
constraint_violation | output_format | insufficient_reasoning | capability_limit |
infrastructure | unknown
```

P1 的 `small_model_executor` 已在 trace 中记录其中三类：`output_format`（未产出文件）、
`insufficient_reasoning`（新文件但验证失败）、`capability_limit`（连续两轮相同文件）。
P2 直接扩展这张表，不新增第二套分类。

### 1.2 Teacher 调用契约（最小）

```python
@dataclass(frozen=True)
class TeacherRequest:
    task_id: str
    failure_class: str
    goal_constraints: list[str]      # 来自 pico/goals.py + goaltree/scientific
    evidence: list[dict]             # 脱敏后的工具收据/验证结果
    allowed_level: str               # 复用 pico/privacy.py: public/general/sensitive/core
    max_output_tokens: int

@dataclass(frozen=True)
class TeacherResponse:
    hypothesis: str
    revised_actions: list[dict]
    verification_conditions: list[str]
    confidence: str                  # low/medium/high，仅记录，不作为正确性
```

规则：
- 默认关闭；开启需显式配置 teacher provider 与 API key。
- `core` / 禁止外发的内容不得进入 `evidence`；由 `pico/privacy.py` 在传输前拦截。
- Teacher 输出**必须**经过真实执行验证才能进入经验池（见 1.3），不能直接当 Gold。

### 1.3 Skill 记录（复用 `pico/evolution.py` 的候选 + 证据闸门）

沿用现有 `EvolutionStore` 的“候选 → 宿主证据闸门 → 激活”模型，新增字段：

| 字段 | 说明 |
|---|---|
| `skill_id` / `version` | 稳定 ID + 递增版本 |
| `applicability` | 任务类型 / 文件模式 / 工具集合 |
| `preconditions` | 何时可用（例如“单文件修复 + unittest 可验证”） |
| `steps` | 可执行步骤（工具名 + 参数模板） |
| `verify` | 独立验证命令或检查 |
| `failure_conditions` | 何时必须放弃该 Skill |
| `source_trace` | 来源 run / task / verifier 结果 |
| `independent_test` | 该 Skill 在留出任务上的独立测试记录 |

激活门槛（建议）：在同一留出任务集上，开启 Skill 的通过率**不低于**关闭时，
且至少 1 个此前失败的任务转为通过；否则保留为候选，不发布。

## 2. P3：QLoRA SFT（外部 GPU，可选依赖）

数据格式（新增 `training/sft_schema.json`，P3 时落地）：

```json
{
  "task_id": "...",
  "source": {"run_id": "...", "trace_sha256": "...", "verifier": "..."},
  "input": {"prompt": "...", "files": {"...": "..."}},
  "target": {"editable_file": "...", "content": "..."},
  "verified": true,
  "cohort": "train"
}
```

硬性规则：
- 只收 `verified=true` 的样本；Teacher 输出未经执行验证不得入集。
- 训练/留出按任务 ID 隔离；同一任务的近重复变体不得跨集。
- Base 模型不改写；Adapter + 训练配置 + 数据版本 + 评测结果写入 `AdapterRegistry`。
- 无独立评测 → 不允许上线；支持回滚到上一个 approved adapter。
- 本机（4 GB GPU）不执行训练；提供脚本与配置，GPU 执行标记为 Not Run。

## 3. P4：GRPO（仅在 P3 有效且奖励可靠后）

- 奖励来源：外部 verifier（测试通过、约束满足、无越权），**不以 Teacher 主观评分作唯一奖励**。
- 同一任务采样多条轨迹；记录奖励分布与失败模式，检测 reward hacking。
- 训练在 LoRA adapter 上进行；对比 SFT vs SFT+GRPO；同样经过留出评测与注册表审批。
- 没有可靠自动奖励的任务，不进入 GRPO。

## 4. P5：自进化策略与模型路由

路由输入（每条执行记录新增，均可本地计算）：
`task_type`、`student_success`、`failure_class`、`teacher_intervention`、`wall_ms`、
`tokens`、`verify_passed`。

策略候选与判定：
1. 提示/Goal 约束修复：失败集中在 `output_format/planning` 且留出集有同类样本。
2. Skill 更新：同类失败在 2+ 任务重复，且本地 Skill 可覆盖。
3. SFT：跨任务、非格式类失败持续存在，且有 ≥N 条验证样本。
4. GRPO：SFT 已上线且奖励稳定。
5. Teacher 接管：student 连续失败且任务高价值；接管必须记录，不能计入 student 独立成功率。

所有策略更新都必须通过独立留出评测；**LLM 自我判断不能直接触发发布**。

## 5. 与现有代码的对接点

| 能力 | 现有模块 | P2+ 需要的改动 |
|---|---|---|
| 执行记录 | `pico/run_store.py`、trace | 增加 failure_class、teacher 调用记录 |
| 经验/Skill | `pico/evolution.py`、`pico/knowledge.py` | 扩展 Skill 字段与独立测试记录 |
| 隐私 | `pico/privacy.py` | Teacher 传输前按 level/purpose 拦截（已具备机制） |
| 评测 | `pico/evaluation/`、`verify_pristine` | 增加 Student/Teacher/混合三种口径 |
| 模型 | `pico/providers/clients.py` | Teacher 客户端复用 OpenAI-compatible 接口 |
| 训练 | 无 | 新增独立 `training/`（不进入运行时依赖） |

## 6. 硬件与阻塞说明

- 本机：RTX 3050 Ti Laptop 4 GB，无 Linux/CUDA 训练栈。P3/P4 **不可在本机实际训练**。
- 可执行的部分：数据构建、格式校验、CPU 小规模 mock 流程、独立评测脚本。
- 未实测的训练/GRPO 结果一律标记 **Not Run**，不得写成已获得提升。
