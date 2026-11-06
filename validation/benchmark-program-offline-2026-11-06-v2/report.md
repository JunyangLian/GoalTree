# 公开基准与过程基准：离线实施报告

2026-11-06。**本轮完成离线基础实现与验证，模型 API 调用 0 次、新增 Agent 正式评测 0 次、Hugging Face 上传 0 次。** 不修改此前四题横评、历史 Recall、成功率或事后得分。

## 已交付及验证结果

| 项目 | 客观结果 | 解释边界 |
| --- | --- | --- |
| SWE-bench Lite | 固定 revision/Parquet SHA/官方 harness 提交；从 300 个 test 实例分层锁定 5 个候选 | 尚未构建容器、执行空/参考补丁资格检查或运行 Agent；不是官方成绩 |
| 官方验收接入 | 预测 JSONL 白名单导出、唯一 run_id、固定本地数据及源码校验；实际探针返回 docker_cli_missing，未启动验收 | 不回退本项目测试或宿主 Shell；空补丁探针不是 Agent 失败记录 |
| 自建场景 | 12 个独立合成场景，dev/test 各 6，每个 split 各含四类修复及两类跨会话场景 | 全为合成题，不声称 12 个真实开源故障；金标独立审查尚未完成 |
| 资产自检 | 24 次真实 AppContainer 外部验证；12 个故障版均拒绝、12 个参考修复版均接受；每版本合计 36 个测试方法 | 测试方法数不等于断言数或独立任务数；不是 Agent 修复率 |
| 上下文观察器 | CodeTrail 与 ApeCode 真实 read_file 输出均能归一；人为省略一行的后续请求被识别为证据丢失 | 脚本探针，非模型压缩效果；仅归因明确的 read_file 原文片段，search/摘要/语义采纳未评估 |
| 跨会话状态 | 4/4 脚本机制探针通过；两种文件变更场景的旧结构记忆失效；文字记忆候选没有自动确认 | 真正调用现有 KnowledgeStore；未验证模型如何生成、检索或使用永久记忆 |
| GLM 适配 | 标准 API 配置、原生 Tool Calling、reasoning_content 回传及 URL/截断/模型替换的离线合同验证通过 | 尚未实际请求，不保证在线返回格式或精确权重版本 |
| 自动化 | 完整套件 420/420；末次修正后相关回归 55/55；去重覆盖当前全部 424 个已收集测试 | 420+55 有重叠，不相加宣传为 475 个测试；不是一次 424 项的单批执行 |
| HF 导出 | 本地草案含 dev/test JSONL、Dataset Card、证据标签、MIT 许可、资格结果和 SHA256；参考修复不导出 | publication_ready=false；未上传、未建立在线排行榜 |

自动化证据：[`完整套件 JUnit`](benchmark-program-full-2026-11-06.xml)、[`末次回归 JUnit`](benchmark-program-final-regression-v2-2026-11-06.xml)。导出器独立核对两份 JUnit 的 `(classname,name)` 并集覆盖当前收集的 424 项，未知或跳过不补为通过。

## 固定公开来源与选择

数据 revision：`6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`；Parquet SHA256：`7a21f37b8bc179c7db5beeb14e88ac538ba283455c776e6b2535bbfb6e3551b4`。官方 harness：`02e7a74ffd0b707aab73d203fe87bdc7c76afc8e`。

5 个候选为 `psf__requests-863`、`pydata__xarray-3364`、`scikit-learn__scikit-learn-14087`、`pylint-dev__pylint-7993`、`pytest-dev__pytest-7168`。选择只使用仓库分组、排序与种子 20261106，没有看 Agent 结果；尚未声称容器资格就绪。详见 [`public-selection.json`](public-selection.json)。

Agent 输入字段限定 `instance_id/repo/base_commit/problem_statement`；上游 patch/test_patch/hints/验收标签不交给 Agent。原始数据与控制器验收副本保留在本地 ignored artifacts，不随本报告分发参考补丁。

## 问题修复与保留

1. 实际 ApeCode 工具探针首次缺少 `ToolContext.ask_approval` 必填参数，未完成。补齐拒绝式回调后另建目录验证，未覆盖首次失败。
2. 智谱标准地址原本被通用规范化逻辑错误追加 `/v1`。现保留 `/api/paas/v4`，并通过 CodeTrail 实际 `complete()` 请求 URL 断言检查；原生推理和工具结果继续完整回传，没有 DSML 文本执行后门。
3. 首次末次回归命令误引不存在的 `tests/test_streaming.py`，退出码 4、0 项运行；修正文件列表后 55 项通过。原日志保留，不把错误命令当测试失败或通过。
4. 首次打包草案把 36 个测试方法字段命名为 assertions，最终 v2 改为 `test_methods_per_version`；原草案本地保留。这是记录口径修正，没有改变验收资产、原始测试结果或金标。

## 当前未完成部分

- 本机无 Docker CLI/可用 Linux 容器。SWE-bench 官方环境、镜像 digest、空/参考补丁自检及容器内 Agent 推理入口尚未就绪。
- 观察器、阶段迁移及两种 GLM 适配已分别实现，但新的多会话双 Agent 模型控制器还需整体串联和实际入口预检；不是已经能按一个命令启动全部 34 次正式试验的交付。
- 证据标签仍是控制器编写的必要文件/源码代理，需要独立审查与等价修复路径核对。没有可靠观测的记忆采纳、语义忠实性或安全攻击效果不计分。
- 用户已明确选择本轮先离线。智谱 Coding Plan 限定指定工具，自建应用需标准 API；未使用聊天提供的套餐凭据，也未伪装工具身份。真实评测还需可用于标准 API 的本地配置、费用上限、实际协议预检及完整冻结。
- 本地 HF 草案暂不公开发布；仍需完成两个 Agent 的受控运行、独立验收、失败记录及质量复核。没有新增通过率、排名或综合总分。

## 证据与复现

[`实施与命令`](../../docs/benchmark-program-offline.md) · [`方案`](../../docs/benchmark-program-v0.1.md) · [`结构化总结`](summary.json) · [`资产资格`](qualification.json) · [`观察器探针`](observer-probe.json) · [`状态迁移探针`](scenario-probe.json) · [`HF 本地草案压缩包`](hf-draft.zip) · [`文件哈希`](manifest.json)。

原始本地证据目录：

```text
artifacts/swebench-lite-selection-2026-11-06/
artifacts/swebench-official-handoff-probe-2026-11-06/
artifacts/processbench-v0.1-assets-2026-11-06/
artifacts/processbench-v0.1-qualification-2026-11-06/
artifacts/processbench-observer-probe-2026-11-06/       # 初始失败目录
artifacts/processbench-observer-probe-v2-2026-11-06/
artifacts/processbench-observer-probe-v3-2026-11-06/
artifacts/processbench-scenario-probe-2026-11-06/
artifacts/processbench-hf-draft-2026-11-06/
artifacts/processbench-hf-draft-v2-2026-11-06/
```

[`历史哈希校验`](historical-evidence-check.json)确认原 DeepSeek 横评和 90 次评测证据包不变。源码指纹见 [`source-hashes.json`](source-hashes.json)，用于核对本次实现，不冒充正式模型实验的冻结协议。

官方依据：[智谱模型](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)、[Coding Plan 使用范围](https://docs.bigmodel.cn/cn/coding-plan/faq)、[SWE-bench 官方验收](https://www.swebench.com/SWE-bench/guides/evaluation/)、[Hugging Face Dataset Cards](https://huggingface.co/docs/hub/datasets-cards)。
