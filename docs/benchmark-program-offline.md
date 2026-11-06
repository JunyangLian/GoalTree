# 基准接入与过程评测：离线运行指南

2026-11-06。当前是基础实现与离线验证，**没有新增模型成绩，没有在线排行榜，尚未上传 Hugging Face**。整体方案见[设计稿](benchmark-program-v0.1.md)。

## 已实现

1. SWE-bench Lite 固定 revision/hash 读取、分层确定性抽样、输入字段白名单、控制器专用验收资产、官方预测 JSONL 导出，以及固定提交官方 harness 的调用入口。缺少 Linux Docker 时记录 blocked，不改用本项目 unittest 代替官方标准。
2. 12 个新合成场景：dev/test 各 6，分别包含单文件、跨文件、定位、兼容、记忆复用和过期记忆。每题 3 个验收方法；参考修复保留在控制器，自检不等于 Agent 评测。
3. 跨会话工作区生命周期：每次场景全新目录；场景内部保持同一工作区所有权，新建会话 ID，控制器按冻结资产替换代码并记录干预前后哈希；来源修改、错会话、错任务、重复推进及越界链接均拒绝。不会自动批准记忆、复制其他运行的记忆或导入旧模型对话。
4. CodeTrail/ApeCode 共用的版本化读取与实际请求观察器：工具回包中真实返回的源码行，只有被同一 tool_call_id 的实际请求携带，才计作送达；区分版本、截断、首次编辑与重复读取。精确片段代理尚未覆盖 search、摘要改写或语义记忆采纳，不能称为完整上下文召回。
5. `glm-5.3-flash` 标准 API 适配与原生消息合同：Thinking 开启、保留 reasoning_content、拒绝模型替换和截断工具调用；CodeTrail 复用现有原生工具状态机，ApeCode 提供公开 ChatModel 协议适配。仅离线响应夹具验证，尚未实际请求确认。
6. Hugging Face 兼容的本地 JSONL 草案导出，包含 Dataset Card、MIT 许可、来源、划分、独立证据标注、资格检查及哈希。未导出参考修复，不自动上传。

## 公开基准准备

环境需要 `pyarrow`（仅准备数据时），正式官方验收需要 Linux Docker 和安装了固定提交官方 harness 的独立 Python 环境。不要在当前 Windows 宿主运行任意仓库测试命令来替代容器。

固定来源：

- 数据：`princeton-nlp/SWE-bench_Lite`，revision `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`，test split。
- Parquet SHA256：`7a21f37b8bc179c7db5beeb14e88ac538ba283455c776e6b2535bbfb6e3551b4`。
- 官方 harness commit：`02e7a74ffd0b707aab73d203fe87bdc7c76afc8e`。

将上游 `data/test-00000-of-00001.parquet` 下载到本地控制器目录，再执行：

```powershell
python -X utf8 scripts/prepare_public_benchmark.py --output artifacts/my-public-selection --parquet artifacts/public-benchmark-sources/swebench-lite/test-00000-of-00001.parquet
```

选择使用种子 20261106；先按 repo 分组，排序后确定性打乱，按仓库轮转选 5 题。未使用成功率或补丁内容挑题。当前选出的 5 个**候选**是 requests-863、xarray-3364、scikit-learn-14087、pylint-7993、pytest-7168。它们尚未通过镜像/空补丁/参考补丁资格验证，因此不是已经就绪的正式冻结集，也没有替换失败题。

只有 `agent-inputs.json` 的白名单信息可以交给 Agent；`verifier-only.json` 含上游参考与测试补丁，仅留在控制器。故障仓库应固定 base_commit，Agent 不能访问修复提交、控制器或其他运行。

预测文件需要每个候选一个记录，失败保留空补丁，不允许缺题或重复 ID。正式运行按 Agent 和 repeat 分文件提交；每次验收使用新目录，唯一 run_id 防止旧结果缓存：

```powershell
python -X utf8 scripts/prepare_public_benchmark.py --output artifacts/my-public-selection --predictions artifacts/my-predictions.jsonl --harness artifacts/public-benchmark-sources/SWE-bench --python path/to/official-harness-python --verification-output artifacts/my-new-official-verification
```

该命令执行的是官方验收交接，不是 Agent 推理调度。它会验证 harness HEAD/干净状态/实际导入位置；进程退出码不能直接当修复成功率，须解析并复核官方逐题测试报告。首次模型运行前，还需完成隔离推理入口、镜像资格验证、费用上限及正式协议冻结。

## 自建资产与资格检查

所有输出目录都必须新建。以下命令**不会调用模型 API**，但会真实执行受 AppContainer 隔离的 Python 测试：

```powershell
python -X utf8 scripts/build_processbench_assets.py --output artifacts/my-processbench-assets
python -X utf8 scripts/validate_processbench.py --assets artifacts/my-processbench-assets --output artifacts/my-processbench-qualification
python -X utf8 scripts/export_processbench_dataset.py --assets artifacts/my-processbench-assets --qualification artifacts/my-processbench-qualification --output artifacts/my-processbench-hf-draft
```

`tasks.json` 含场景、可见验收、阶段 A 文件、阶段 B 文件和证据标签；`controller-only-reference.json` 只供资产自检。Agent 只能安装当前阶段的 `files` 与接收 `prompt`，不能把整行数据、gold 或控制器参考拼进上下文。`asset-lock.json` 是资产锁，**不是包含完整运行环境与 Agent 实现的正式实验冻结**。

阶段管理入口是 `pico.evaluation.process_scenario.start/advance`，观察器是 `ProcessObserver.tool_result/outbound_request/finish`。外部观察方应通过 `read_bytes().decode('utf-8')` 提供原文件文本以保留换行版本，且只给 `outbound_request` 压缩后实际发出的消息。两个 Agent 的模型回调及工具回调还需在同一新控制器中完成整体串联，不能把这些观察器单元/集成检查说成已完成付费横评。

真实读取工具的离线集成探针需要按上游要求使用 Python 3.13，且 ApeCode 源码固定 `8d85a3929cac72665e96f10d1dcbcce2962ca49b`：

```powershell
python3.13 -X utf8 scripts/check_process_observers.py --output artifacts/my-observer-check --apecode-source artifacts/cross-agent-sources/apecode
python -X utf8 -m pytest -q --junitxml=artifacts/my-full-tests.xml
```

集成探针真实读取两个 Agent 的工具输出，并人为从下一轮请求省略一行，检查观察器能否检测。决策及遗漏是脚本安排，不是模型压缩效果。首次探针缺少上游 ToolContext 的必填 ask_approval 参数，已修复；原失败目录保留，后续独立目录通过。

## 模型配置和当前限制

智谱官方确认 `glm-5.3-flash` 支持原生 Function Calling，Thinking 不能关闭。暂定按官方推荐采用 temperature=1、top_p=0.95、reasoning_effort=max、clear_thinking=false；非流式便于两端统一采集。真实预检确认前不得宣称返回协议或权重版本已验证。

Coding Plan 仅限官方指定工具，CodeTrail/ApeCode 不在当前名单，自建应用需标准 API。当前适配器只使用标准端点 `https://open.bigmodel.cn/api/paas/v4` 和本地 `BIGMODEL_API_KEY`；未使用聊天中提供的套餐密钥，也不伪装工具身份、不静默改走付费余额。本轮按用户选择仅离线验证。

费用没有实际账单则未知；订阅价格不能当作一次运行费用。后续真实运行需标准 API、明确金额上限及实时/保守预算门控，缺失 Token 不补零。

首版自建证据是控制器编写的代理标签，独立人工审查尚未完成；候选 12 题均为合成题，真实上游故障覆盖由公开基准另外承担。跨会话源码仍可重读，因此最终通过不证明记忆收益。没有安全攻击新成绩，没有新的 Agent 成功率，没有综合总分。

Hugging Face 草案尚未上传；正式发布前需补齐证据审查、两个 Agent 的完整受控运行、失败与独立验收、资产许可及脱敏核对。测试集据此调优后须标为已知回归集。

官方来源：[模型参数](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)、[套餐用途限制](https://docs.bigmodel.cn/cn/coding-plan/faq)、[指定工具与接口](https://docs.bigmodel.cn/cn/coding-plan/tool/others)、[SWE-bench 验收](https://www.swebench.com/SWE-bench/guides/evaluation/)、[Dataset Cards](https://huggingface.co/docs/hub/datasets-cards)。
