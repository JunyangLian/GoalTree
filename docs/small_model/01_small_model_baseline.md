# 01 · 小模型基线与实验设置（P0）

## 1. 模型与运行环境（实测）

| 项 | 值 | 证据 |
|---|---|---|
| 模型 | `qwen2.5-coder:1.5b-local`（Qwen2.5-Coder-1.5B-Instruct，Q4_K_M GGUF，986 MB） | `ollama list`；由已下载 GGUF 用本地 Modelfile 创建 |
| 推理后端 | Ollama（`http://127.0.0.1:11434`），`/api/generate`，text 协议 | `pico/providers/clients.py` |
| 设备 | Windows 11，Python 3.10.1，RTX 3050 Ti Laptop 4 GB | `python --version`、`nvidia-smi` |
| 运行方式 | 100% GPU（Ollama 报告），temperature 0.3，top_p 0.9，repeat_penalty 1.15 | 运行参数 |
| 备注 | 官方 registry 的 manifest 拉取多次超时；已下载的 blob 完整，故用本地 Modelfile 导入。**未安装 Docker/WSL/CUDA 训练栈** | `ollama pull` 日志、`ollama create` 输出 |

单次最小请求：43 输入 token / 8 输出 token，约 24 s（含首次加载）；正式任务约 20–60 s/次调用。

## 2. 任务集（新增 `benchmarks/small_model_v1`）

- 8 个单文件修复任务，每个含 1 个待修模块 + 1 个冻结测试文件（3–6 个 unittest 断言）。
- 划分：**dev 5**（clamp_value、slugify_title、parse_env_line、dedupe_keep_order、chunk_list）；
  **holdout 3**（merge_counts、format_bytes、top_k）。
- 验证方式：`python -B -m unittest discover -v`，并由 `verify_pristine` 在**冻结副本**上重装文件与测试，
  只允许覆盖 `editable_files`。模型改测试文件不会通过外部验证。
- 预检（无模型）：8/8 任务 buggy 必失败、reference 必通过，`tests_run` 与 `expected_tests` 一致。

## 3. 对照模式（A/B/C 及补充）

| 代号 | 模式 | 说明 |
|---|---|---|
| A | `oneshot` | 单次模型调用，无工具；输入含待修文件与测试文件，输出必须是一个完整代码块 |
| B | `agent` | Pico/GoalTree 经典 prompt + 完整工具集（list/read/write/patch/run_shell），审批只允许写目标文件与运行验证命令 |
| C | `agent-compact` | 小模型档位：短 prompt + 仅 read/write/run_shell + 完成拒绝时给出**字面**验证调用 |
| D | `agent-repair` | Pico + 写盘后由宿主自动执行真实验证（`auto_verify_after_write`） |
| E | `agent-structured` | Pico 结构化 profile：模型只回完整文件，宿主写盘并自动验证；请求携带当前目标文件（`structured_repair_path`） |
| C' | `scaffold` | Harness 级修复回路：模型只输出完整文件，Harness 写盘并运行真实测试，把失败输出回贴，最多 2 轮 |

C' 单独记录，**不与 Pico agent 混算**：它验证“去掉工具协议负担、保留真实反馈”是否有效。

## 4. 基线成绩

> 原始报告：`D:\1Bio_Soft\small_model_p0_p1\report.json`（dev）、
> `D:\1Bio_Soft\small_model_agents\report.json`（agent 两档）、
> `D:\1Bio_Soft\small_model_holdout\report.json`（holdout）。均在仓库外。

dev（3 题，k=1）：

| 模式 | 通过 | 均值 wall |
|---|---|---|
| `oneshot` | 1/3 | 31.6 s |
| `agent` | 0/3 | 148.8 s |
| `agent-compact` | 0/3 | 310.6 s |
| `agent-repair`（Pico + 自动验证） | 0/3 | 312.1 s |
| `agent-structured`（Pico 结构化修复） | 1/3 | 237.6 s |
| `scaffold` | 3/3 | 59.7 s |

holdout（3 题，k=1）：

| 模式 | 通过 | 均值 wall |
|---|---|---|
| `oneshot` | 1/3 | 42.8 s |
| `agent-structured` | 2/3 | 235.1 s |
| `scaffold` | 2/3 | 51.4 s |

`agent` / `agent-compact` / `agent-repair` 在 holdout 上 **Not Run**（成本原因，且 dev 已 0/3）。
`agent-structured` 的原始逐运行记录在 `D:\1Bio_Soft\small_model_structured\runs.jsonl`；
失败模式与诚实边界见 `02_harness_optimization.md`。

## 5. 复现命令

```bash
# 资产（已生成，重跑会覆盖）
python -X utf8 scripts/build_small_model_assets.py

# 预检（无模型）
python -X utf8 -c "from pico.evaluation.repair_suite import verify_pristine; ..."

# dev 消融
python -X utf8 scripts/run_small_model_eval.py \
  --model qwen2.5-coder:1.5b-local \
  --modes oneshot,agent,agent-compact,scaffold \
  --cohort dev --tasks clamp_value,slugify_title,chunk_list \
  --k 1 --max-steps 4 --max-new-tokens 512 --run-timeout 240 --scaffold-rounds 2 \
  --out D:/1Bio_Soft/small_model_p0_p1
```

## 6. 诚实边界

- 小模型单次调用质量、工具协议遵循度都不是本仓库能控制的；每次结论只来自实际运行。
- 未跑官方 80% 相对能力目标所需的 Teacher 基线与更大留出集；本轮不做该声称。
- Ollama 模型是本地 GGUF 导入，不是 registry 上可校验的官方 manifest；版本以 blob SHA256 记录。
