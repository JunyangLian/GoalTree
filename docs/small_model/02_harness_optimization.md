# 02 · 小模型 Harness 优化（P1）

## 1. 真实失败模式（先测再改）

以下都是本机实际运行记录（`qwen2.5-coder:1.5b-local`，单任务，k=1）：

| 观察 | 现象 |
|---|---|
| one-shot（clamp_value） | 1 次调用 32.5 s，通过 4/4；**但返回内容同时包含测试文件**，首版抽取逻辑把两份文件拼在一起写盘，只有 1 个测试被发现（已修正抽取：遇到 `import unittest` / `*Tests` 就截断） |
| one-shot（slugify_title） | 模型把“去首尾符号”放在正则替换之前，结果 `C++ & Python!` → `c-python-`，测试失败；属模型内容错误 |
| `agent`（classic，clamp_value） | 10 次工具调用：1 次 `write_file` + 9 次 `patch_file`，**从未运行验证命令**；458 s 后被完成门拒绝，测试仍失败 |
| `agent-compact`（clamp_value，第一版） | 1 次工具调用后连续输出非结构化文本，9 次模型调用 513 s，无验证；随后开启 repeat penalty，仍出现 `HTTP 500 prediction aborted, token repeat limit reached` |
| `agent-compact`（加上字面验证调用后） | 模型确实执行了验证命令，但没有写修复（1 次工具调用），310 s 后测试失败 |
| `scaffold`（clamp_value） | 1 轮即通过，33.4 s |

结论：小模型的主要瓶颈不是“不会改这一行代码”，而是 **text 工具协议与多步自管理**。
去掉协议后仍会暴露第二层瓶颈：**模型收到真实失败输出后原样重发同一份错误实现**（见 `agent-structured`）。

## 2. 本轮改动（全部 opt-in，默认路径不变）

| 改动 | 位置 | 作用 |
|---|---|---|
| `compact_prompt` | `pico/prompt_prefix.py`、`pico/runtime.py`、`pico/cli.py` | 短规则（协议 + 工具 + 工作区），去掉长规则与多余示例 |
| 完成拒绝时给出**字面**验证调用 | `pico/completion.py` | 仅 `compact_prompt=True` 时生效；把 `<tool>{"name":"run_shell",...}</tool>` 原样给出 |
| 小模型工具子集 | `scripts/run_small_model_eval.py` | compact 模式只给 `read_file/write_file/run_shell`，移除 `patch_file`（阻断 patch 循环） |
| Ollama usage 回填 | `pico/providers/clients.py` | `prompt_eval_count/eval_count` → `last_completion_metadata` |
| Ollama 重复控制 | `pico/providers/clients.py` | 新增 `repeat_penalty/repeat_last_n` 选项 |
| Harness 级修复回路 | `scripts/run_small_model_eval.py`（`scaffold`） | 模型只输出完整文件；Harness 写盘、跑真实测试、回贴失败输出，最多 2 轮 |
| **Pico 内建自动验证** | `pico/agent_loop.py`（`auto_verify_after_write`）、`pico/runtime.py`、`pico/cli.py` | 写盘成功后由宿主执行真实验证命令，输出并入同一观察；完成门仍要求 exit 0 |
| **Pico 结构化修复** | `pico/agent_loop.py`（`structured_repair_path`）、`pico/runtime.py`、`pico/cli.py` | 模型回复纯代码块时，按目标文件交给 `write_file`；不要求 `<tool>` 语法，审批/沙箱/完成门不变 |
| **结构化请求携带目标文件** | `scripts/run_small_model_eval.py`（`agent-structured`） | 该 profile 禁用工具，请求里必须带当前文件内容；否则 1.5B 会凭记忆重写并丢掉原有校验 |

`auto_verify_after_write` 的边界：只对 `write_file/patch_file/apply_changes` 的成功结果触发；
命令就是配置的 `verification_command`，仍走原有审批；不改完成标准，也不代替模型修复。

`agent-structured` 的请求只带“当前目标文件”，**不带测试文件**。尝试过把可见测试也放进请求（v2），
结果更差：slugify 772 s 失败、merge_counts 由 ✅ 变 ❌、chunk 321 s 失败——模型会把测试内容回贴进模块。
该变体已放弃，保留 v1 请求格式。`format_bytes` 有一次 `FileExistsError` 属于重跑前的残留目录，
该行不是模型运行结果，删除目录后已重跑（见下文 536 s 的正式记录）。

`scaffold` **不是 Pico 的主循环**，它是本轮用于验证“去掉工具协议负担后，真实反馈是否足够”的对照臂；
如果有效，下一阶段再以 `compact_prompt` 之外的 profile 形式并入 Pico，而不是另起一套 Agent。

## 3. 消融结果（dev，3 个任务，k=1）

原始 JSON：`D:\1Bio_Soft\small_model_p0_p1\report.json`（one-shot/scaffold）与
`D:\1Bio_Soft\small_model_agents\report.json`（Pico agent 两档），均在仓库外。
`agent-structured` 的逐运行记录：`D:\1Bio_Soft\small_model_structured\runs.jsonl`
（该目录的 `report.json` 只覆盖最后一次单题进程，不用于汇总）。

| 模式 | 通过 | 均值 wall | 运行验证命令 | 备注 |
|---|---|---|---|---|
| `oneshot` | **1/3** | 31.6 s | — | clamp 通过；slugify/chunk 为内容错误 |
| `agent`（classic） | **0/3** | 148.8 s | 1/3 | 每题 4 次工具调用后结束；patch/写盘反复，未验证 |
| `agent-compact` | **0/3** | 310.6 s | 2/3 | 每题 2–3 次工具调用、10–12 次模型调用（协议重试） |
| `agent-repair`（Pico + 自动验证） | **0/3** | 312.1 s | 1/3（自动） | 2 题在 12 次模型调用中**没有产出任何合法工具调用**；1 题写了文件并触发自动验证，但仍未修对 |
| `agent-structured`（Pico 内建结构化修复） | **1/3** | 237.6 s | 3/3（自动） | clamp 2 次模型调用、31.0 s 通过；slugify/chunk 各 11 次调用内**重复同一份错误代码**，步数耗尽 |
| `scaffold`（Harness 回路） | **3/3** | 59.7 s | Harness 自动 | 最多 2 轮；clamp 1 轮、slugify/chunk 各 2 轮 |

逐题：

| 任务 | oneshot | agent | agent-compact | agent-repair | agent-structured | scaffold |
|---|---|---|---|---|---|---|
| clamp_value | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ |
| slugify_title | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| chunk_list | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |

解读：
- 在这个 1.5B 模型上，**工具协议是主要成本**：两档 agent 的 token 足够（7k–13k 输入），但没有把“写对文件 + 跑验证 + 修到通过”串起来。
- `compact` 的字面验证提示确实让模型运行了验证命令（2/3），但没有转化为通过；模型调用次数反而更多（协议重试）。
- **把自动验证并进 Pico 也没有解决根本问题**：`agent-repair` 的 3 题里有 2 题在 12 次模型调用内没有产出任何合法工具调用，自动验证根本没有机会触发。
- **`agent-structured` 首次让 Pico 主循环自己“写对 + 验证通过 + 完成”**：clamp 2 次模型调用、2 次工具调用、31.0 s 通过；不再是 patch 循环或空转。
  但它只解决“输出格式”，不解决“重复错误答案”：slugify/chunk 收到真实失败输出后仍原样重发同一份代码，11 次调用后步数耗尽。
- `scaffold` 去掉工具协议，只保留“完整文件 + 真实失败输出”，3/3 通过；仍是本轮最强对照臂。

holdout（3 题，k=1，未参与调参；agent 两档与 agent-repair 未跑，标 Not Run）：

| 模式 | 通过 | 均值 wall | 逐题 |
|---|---|---|---|
| `oneshot` | **1/3** | 42.8 s | merge_counts ✅ / format_bytes ❌ / top_k ❌ |
| `agent-structured` | **2/3** | 235.1 s | merge_counts ✅（146.6 s）/ format_bytes ❌（536.1 s）/ top_k ✅（22.7 s） |
| `scaffold` | **2/3** | 51.4 s | merge_counts ✅ / format_bytes ❌ / top_k ✅ |

`scaffold` 从 dev 3/3 降到 holdout 2/3：**方向一致但不是“稳定解决”**；format_bytes 在两档都失败（模型对 1024 进制与 B/KB 边界处理错误）。
`agent-structured` 在 holdout 上也是 2/3，且 format_bytes 同样失败；但两题通过时都只有 2 次模型调用（22.7 s / 146.6 s），
说明“去掉工具协议”本身能让 1.5B 一次写对，**失败集中在模型反复重发同一份错误实现**，而不是 Harness 执行链。


## 4. 解释与诚实边界

- 3 个 dev 任务、k=1，**不足以给出泛化结论**；holdout 只在最后测量，不参与调参。
- `agent` / `agent-structured` 与 `scaffold` 不是同一层机制：前两者是真正的 GoalTree/Pico 循环
  （工具审批、写盘、完成门、trace 全在环内），后者是 Harness 级回路。报告里分列，不能把 `scaffold`
  的成功算成“Pico 主循环变强”；`agent-structured` 的通过可以算 Pico 主循环，但样本只有 3+3 题、k=1。
- 小模型在 text 协议下的不稳定（重复 token、非结构化输出）是模型行为，不是 Harness 能完全消除的；
  repeat penalty 只是缓解。
