# 00 · 架构审查与最小改造范围（GoalTree 2.0 / P0–P1）

审查对象：本仓库当前 HEAD（`05ce173`），Windows 11、Python 3.10.1、RTX 3050 Ti Laptop 4 GB。
本轮只做 P0（本地小模型基线）与 P1（Harness 优化）；P2–P5 只确定接口方向，不实现训练。

## 1. 现有架构与关键代码位置

| 层 | 位置 | 现状（源码确认） |
|---|---|---|
| CLI / 入口 | `pico/cli.py` | 支持 `deepseek` / `openai` / `anthropic` / `ollama` 四类 provider；负责构造 `Pico`；本轮新增小模型相关 opt-in 参数 |
| 运行时 | `pico/runtime.py` | `Pico.ask()` → `AgentLoop`；持有 prompt prefix、工具注册表、session/checkpoint、`verification_command`、`allowed_tools`、`harness_profile`、`scientific_verification`、`compact_prompt`、`small_model_executor` |
| 主循环 | `pico/agent_loop.py` | `CompletionGate` 完成判定；text 协议（`<tool>{json}</tool>` / `<final>`）；工具执行、trace、safety；本轮之前的科学约束闸门挂点也在这里 |
| 工具与权限 | `pico/tools.py`、`pico/tool_executor.py`、`pico/environment.py` | 工具注册、参数 schema、审批回调、路径越界检查、shell 环境 allowlist、AppContainer 可选 |
| 模型客户端 | `pico/providers/clients.py` | `OpenAICompatibleModelClient`、`AnthropicCompatibleModelClient`、`OllamaModelClient`；Ollama 走 `/api/generate`（text 协议，无原生 tool calling） |
| Goal / Task | `pico/goals.py`、`pico/goal_instructions.py` | `GoalStore`、Goal/Task 约束与 executor/coordinator 指令；GoalTree 的“目标驱动”核心 |
| 记忆 / 知识 | `pico/knowledge.py`、`pico/evolution.py`、`pico/shared_memory.py`、`pico/retrieval.py` | `KnowledgeStore`；`EvolutionStore`（文本 Skill 候选 + 宿主证据闸门，无模型/无 shell）；共享记忆与检索 |
| 隐私 | `pico/privacy.py` | 四级标签 `public/general/sensitive/core`；`model/embedding/search/mcp/web` 用途；传输前检查；不可由模型降级 |
| 自改进（运维） | `pico/improver.py`、`pico/flywheel.py`、`pico/improvement_cases.py` | 低频、操作员触发的提案；本地聚合指标；不上传私有数据 |
| 科学约束 | `goaltree/scientific/` | 编译指令中的显式统计要求；证据感知验证；只有阳性漂移才 blocking |
| 评测 | `pico/evaluation/`、`benchmarks/`、`scripts/run_*` | repair 套件（`install`、`inventory`、`verify_pristine`）、final-agent 30 题、goal autonomy 等；外部验证器独立于 Harness |
| 断言/验收 | `scripts/repair_external_verifier.py` | 在冻结副本上跑 unittest，返回 `tests_run`，用于拒绝“改测试骗过”的结果 |

## 2. 已有能力与本次目标的对应

| 目标 | 现有可复用能力 | 缺口 |
|---|---|---|
| P0 本地小模型 | `OllamaModelClient` + `--provider ollama --model ...`；usage/延迟/trace 已有落点 | 无小模型专用任务集；Ollama 不回填 token（本轮已补） |
| P1 Harness 优化 | `build_prompt_prefix`、`allowed_tools`、`verification_command`、审批回调 | 经典 prefix 对小模型过长；无“小模型档位” |
| P2 Teacher 指导 | 审批/隐私/脱敏、`EvolutionStore` 证据闸门、trace 完整 | 无 teacher 客户端契约、无失败分类器 |
| P3 SFT/LoRA | 无 | 训练依赖、数据格式、Adapter 注册表 |
| P4 GRPO | 无 | 可验证奖励与多轨迹采样 |
| P5 路由 | 无 | 能力/成本/失败率记录 |
| 独立评测 | `verify_pristine` 外部验证、冻结任务 JSON | 无小模型基线脚本（本轮新增） |

## 3. 可直接复用（不改语义）

- Pico 主循环、工具、审批、路径沙箱、trace/checkpoint、session。
- `OllamaModelClient`（text 协议，正好匹配小模型的 `<tool>` 输出方式）。
- `pico.evaluation.repair_suite.install / verify_pristine / VERIFY_COMMAND`：外部验证器会重新安装冻结文件与测试，只覆盖允许编辑的文件。
- `benchmarks/*/tasks.json` 的字段风格（`files`、`prompt`、`editable_files`、`expected_tests`、`cohort`）。
- `pico/privacy.py` 的等级与用途控制；`pico/evolution.py` 的候选 Skill + 证据闸门思路。
- `pico/flywheel.py` 的本地指标存储（不联网）。

## 4. 需要增强（本轮已做）

1. **小模型 prompt 档位**：`pico/prompt_prefix.py` 新增 `compact=True` 分支（约 15 行规则），仅保留协议、工具和验证要求；`pico/runtime.py` 新增 `compact_prompt=False`，`pico/cli.py` 新增 `--compact-prompt`。默认路径完全不变。
2. **Ollama 用量回填**：`OllamaModelClient.complete` 现在把 `prompt_eval_count/eval_count` 写进 `last_completion_metadata`，P0 才能记录 token。
3. **小模型评测**：新增 `benchmarks/small_model_v1/`（8 题：5 dev / 3 holdout）与 `scripts/run_small_model_eval.py`（七种模式：`oneshot` / `agent` / `agent-compact` / `agent-repair` / `agent-structured` / `small-executor` / `scaffold`）。
4. **Pico 内建小模型执行器**：`small_model_executor` 在 `AgentLoop` 内以宿主驱动多轮执行，每轮重建请求，写盘仍走审批/沙箱/完成门。
5. **任务资产构建**：`scripts/build_small_model_assets.py` 生成冻结任务与参考解；预检要求“buggy 必失败、reference 必通过”。

## 5. 不可避免的新依赖

- 本轮：**无新增 Python 依赖**。
- P0 运行依赖：Ollama（本机已运行，仅安装了 1 个 1.5B GGUF，无 Docker/WSL/CUDA 安装）。
- P3 起：`peft`/`trl`/`bitsandbytes` 等只在独立 Linux GPU 环境中可选安装，不进入 GoalTree 普通运行依赖。

## 6. 最小改造方案与影响范围

| 文件 | 改动 | 默认行为影响 |
|---|---|---|
| `pico/prompt_prefix.py` | 新增 `compact` 参数与短模板 | 无（默认 `False`） |
| `pico/runtime.py` | 新增 `compact_prompt` 参数 | 无（默认 `False`） |
| `pico/cli.py` | 新增 `--compact-prompt` / `--auto-verify-after-write` / `--structured-repair-path` / `--small-model-executor` / `--small-model-rounds` / `--small-model-reference-paths` | 无（默认关闭） |
| `pico/agent_loop.py` | 小模型 scaffold：写盘成功后**自动执行真实验证**并把输出并入同一观察（`auto_verify_after_write`，默认关闭） | 无（默认 `False`） |
| `pico/agent_loop.py` | 结构化修复：回复若是纯代码块，按目标文件内容交给 `write_file`（`structured_repair_path`，默认关闭）；审批/沙箱/完成门不变 | 无（默认 `None`） |
| `pico/agent_loop.py` | 小模型执行器：宿主驱动多轮，每轮以“任务 + 当前文件 + 可见测试 + 最新失败输出”重建请求；重复文件早停（`small_model_executor`，默认关闭，要求目标文件与验证命令） | 无（默认 `False`） |
| `pico/providers/clients.py` | Ollama 回填 token 元数据；新增 `repeat_penalty/repeat_last_n` | 仅新增元数据/选项 |
| `scripts/build_small_model_assets.py` | 新资产构建脚本 | 新增文件 |
| `scripts/run_small_model_eval.py` | 新评测脚本 | 新增文件 |
| `benchmarks/small_model_v1/*` | 8 题冻结任务 + 参考解 + 协议 | 新增资产 |
| `tests/test_small_model_harness.py` | 9 个测试（含截断、纯代码块、自动验证、structured prompt） | 新增测试 |

风险与边界：
- 小模型在 text 协议下的工具调用成功率未知，必须先测；不能把“模型自述完成”当成功，全部以外部 verifier 为准。
- Windows 上 Ollama 走 CPU/4 GB GPU 混合，速度和显存占用需实测记录。
- 官方 80% 相对能力目标需要 Teacher 基线与独立 holdout，本轮不宣称达到。
