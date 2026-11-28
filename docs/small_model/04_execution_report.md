# 04 · 本轮执行报告（P0 + P1 第一版）

日期：2026-11-28。执行环境：Windows 11、Python 3.10.1、RTX 3050 Ti Laptop 4 GB、Ollama 本地服务。

## 1. 已完成并实际运行

| 项 | 结果 | 证据 |
|---|---|---|
| 本地小模型接入 | `qwen2.5-coder:1.5b-local`（Qwen2.5-Coder-1.5B-Instruct Q4_K_M，986 MB）可推理，100% GPU | `ollama list`、`ollama ps`、最小请求 43/8 token |
| 真实 GoalTree 任务 | Pico 主循环实际执行 3 个单文件修复任务（非单次聊天） | `runs/agent-*`、trace、session |
| 任务集 | 8 题（5 dev / 3 holdout），冻结任务 + 测试 + 参考解 | `benchmarks/small_model_v1/` |
| 预检 | 8/8 buggy 失败、8/8 reference 通过 | `verify_pristine` 输出 |
| one-shot 基线 | dev 3 题 **1/3**（clamp 通过；slugify/chunk 失败，均为模型内容错误） | `report.json`（工作区外） |
| `scaffold`（Harness 级回路） | dev 3 题 **3/3**，均值 59.7 s；clamp 1 轮、slugify/chunk 各 2 轮 | 同上 |
| `agent` / `agent-compact`（Pico 主循环） | `agent` **0/3**（均值 148.8 s，验证 1/3）；`agent-compact` **0/3**（均值 310.6 s，验证 2/3） | `D:\1Bio_Soft\small_model_agents\report.json` |
| `agent-repair`（Pico + 写盘后自动验证） | **0/3**（均值 312.1 s）；2 题 12 次模型调用内**无合法工具调用**，仅 1 题触发自动验证且未修对 | `D:\1Bio_Soft\small_model_repair\report.json` |
| `agent-structured`（Pico 内建结构化修复） | dev **1/3**（均值 237.6 s，3/3 自动验证）；holdout **2/3**（均值 235.1 s）；通过时均 2 次模型调用 | `D:\1Bio_Soft\small_model_structured\runs.jsonl` |
| holdout（独立留出，未调参） | `oneshot` **1/3**、`agent-structured` **2/3**、`scaffold` **2/3**（format_bytes 三条臂均失败） | `D:\1Bio_Soft\small_model_holdout\report.json`、`small_model_structured\runs.jsonl` |

## 2. 已完成并实际运行的修正（本轮内部问题）

| 问题 | 现象 | 修正 |
|---|---|---|
| one-shot 抽取把两份文件拼在一起 | 模型回贴“模块 + 测试”于同一代码块，写盘后只发现 1 个测试 | `extract_code` 遇到 `import unittest` / `*Tests` 截断；单测覆盖 |
| runner 引用未定义的 `run_agent` | agent / agent-compact 3 题全部 `NameError`，`wall_ms=null` | 恢复函数定义后重跑；作废行不进入统计 |
| 1.5B 在完成拒绝后不按协议调用工具 | classic：10 次 patch、0 次验证；compact：9 次调用、0 次写盘 | compact 模式的拒绝消息给出**字面**验证调用（`pico/completion.py`） |
| Ollama token 重复 | `HTTP 500: prediction aborted, token repeat limit reached` | 新增 `repeat_penalty=1.15`、`repeat_last_n=128` |
| structured 首版丢失原有校验 | 请求只给文件名、不给文件内容，1.5B 凭记忆重写，`clamp` 丢掉 `low > high` 校验，测试 4 个只过 3 个 | 请求中携带“当前目标文件”；修正后 clamp 2 次调用通过（31.0 s） |
| structured 回贴测试段 | 模型在代码块后附 `# Test cases` + `assert`，写盘后模块导入失败 | 抽取时在 `import unittest` / `assert` / `# test` 处截断（单测覆盖） |
| structured 反复重写同一文件 | 代码与当前文件相同且已验证通过时仍继续写，直至步数耗尽 | 视为 final；未通过时保留重写，让真实失败继续可见 |
| 把可见测试也放进 structured 请求（v2） | 结果更差：slugify 772 s、merge_counts 由 ✅ 变 ❌、chunk 321 s | 已回退，保留“只给当前文件”的 v1 请求格式（见 `02`） |

## 3. 仅通过单元测试 / 离线验证

- `tests/test_small_model_harness.py`：9 个用例（compact prompt、Ollama usage、代码抽取含测试段截断、字面验证调用、自动验证、纯代码块写盘、无 fence 代码、structured prompt 不含 `<tool>`、任务预检）。
- 本轮回归：`test_small_model_harness` → **9 passed**（8.6 s）；
  `test_pico + test_prompt_prefix + test_safety_invariants` → **77 passed**（69.5 s）。
- 资产预检：8/8 buggy 失败、8/8 reference 通过（离线，无模型调用）。

## 4. 使用 Mock 验证

- 无。所有模型调用都是真实 Ollama 推理。
- 训练相关（P3/P4）未运行，也未用 mock 冒充训练结果。

## 5. 已设计但未实现

- Teacher Guidance 契约、失败分类、Skill 版本/独立测试记录（见 `03_evolution_roadmap.md`）。
- QLoRA SFT 数据格式、Adapter 注册表、训练门禁。
- GRPO 奖励设计。
- 路由器的能力/成本记录。
- 官方 80% 相对能力目标：需要 Teacher 基线 + 独立留出集 + 更大 n，本轮**未测量、不声称**。

## 6. 因硬件或外部依赖阻塞

- 本机 4 GB GPU、无 Linux/CUDA 训练栈：QLoRA/GRPO **无法在本机实测**。
- Ollama registry manifest 多次超时；已用本地 GGUF 导入绕过，模型来源以 blob SHA256 记录。
- 小模型工具协议遵循度低是模型能力边界，本轮只能通过 Harness 降低负担，不能消除。

## 7. 结论（只写证据支持的）

1. **P0 成立**：GoalTree 能实际驱动 1.5B 本地模型完成真实任务并保存完整轨迹、token、耗时与外部验证结果。
2. **A（one-shot）在简单单文件任务上并不弱**：3 题 1/3；失败是内容错误，不是执行框架问题。
3. **B（Pico classic）在当前 1.5B 上表现差**：dev 0/3，4 次工具调用内结束，只运行了 1/3 次验证；patch 循环与“不验证”是明确失败模式。
4. **C'（scaffold）**：dev 3/3、holdout 2/3——去掉工具协议负担 + 真实测试反馈在本模型上有效；但它不是 Pico 主循环，且 holdout 未满分，**不能据此说 GoalTree 本身变强，也不能说已稳定解决**。
5. **C（compact_profile）只解决了一部分**：验证命令执行率 1/3 → 2/3，但通过率仍 0/3，且模型调用次数增加到 10–12 次（协议重试），wall 翻倍。
5b. **把“写盘后自动验证”并进 Pico（agent-repair）仍是 0/3**：2 个任务在 12 次模型调用内没有产出任何合法工具调用，自动验证无从触发。**协议合规才是瓶颈，不是验证步骤。**
5c. **`agent-structured` 是 Pico 主循环内的第一个正向前进**：dev 1/3、holdout 2/3；通过时 2 次模型调用（clamp 31.0 s、top_k 22.7 s、merge_counts 146.6 s）。
   它证明“模型只输出完整文件、宿主写盘 + 真实验证”可以作为 Pico 的一个 profile 工作；但仍低于 `scaffold` 的 dev 3/3，
   失败模式是**模型收到真实失败后原样重发同一份错误代码**（slugify/chunk/format_bytes 均 11 次调用后步数耗尽），不是执行链故障。
6. 当前证据不足以宣称“不训练就让小模型更可靠”；能站住的是：**在这个模型与这 8 个单文件任务上，协议负担是主要瓶颈，完整文件 + 真实反馈的回路有效（dev 3/3、holdout 2/3）；Pico 内建 structured profile 能部分复现该收益（1/3、2/3）**。更大模型、更多任务、k>1 才能判断边界。
