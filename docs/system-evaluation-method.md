# CodeTrail 系统横评与量化诊断 v2

2026-11-07。评测对象是指定版本的 Agent、模型、工具和适配配置组成的系统。Agent 的语言、内部 Python、提示、上下文压缩和恢复策略可以不同。共同任务、可信外部验收、可解释的资源和权限条件是比较基础；内部实现全部相同不是系统横评的前提。

本轮原配置曾声称所有内部测试统一 Python3.10，实际 CodeTrail 使用3.13，属于必须披露的配置偏差；该事实不使统一外验的最终结果或轨迹指标整体失效。旧数据可以做有边界的系统横评，不能说遵守了旧统一运行时声明，或把差异归因于一个 Harness 模块。ApeCode 的命令约束呈现及截断退出策略也属于所测适配配置，不冒充默认 CLI。

## 参考的方法与本项目适配

下表是公开方法的借鉴，不代表在这些官方数据集上运行或取得对应官方分数。

| 主要来源 | 借鉴内容 | CodeTrail 接入方式 |
|---|---|---|
| [SWE-bench](https://www.swebench.com/SWE-bench/guides/evaluation/) | 补丁由独立测试环境验收 | 功能率、二次验收；PASS_TO_PASS回归保留另需逐测试基线 |
| [Anthropic Agent评测](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) | 最终状态与执行轨迹分开，代码/模型/人工裁判各有适用范围 | 原冻结修复分保持，轨迹诊断单列；不用成功关键词充当忠实性评分 |
| [Terminal-Bench / Harbor](https://github.com/harbor-framework/docs/blob/main/quickstart.mdx) | 每次实验记录配置、轨迹、验收和资源 | 按运行保留Token、耗时、错误及证据哈希；不声称ATIF格式兼容 |
| [AgentBoard](https://hkust-nlp.github.io/agentboard/static/leaderboard.html) | 成功、进展和动作有效性分开 | 最终测试通过比例、接口准入代理、调用重复；这些适配项不是官方Progress/Grounding分 |
| [ToolSandbox](https://github.com/apple-aiml-research/ToolSandbox) | 中间状态与最终状态检查 | 基于文件快照确认编辑生效、同文件补丁纠正；不以计划条目打勾认定完成 |
| [BFCL](https://gorilla.cs.berkeley.edu/leaderboard.html) | 工具调用能力应有专门评价 | 报告参数和未知工具错误；语义工具选择正确率需工具金标，未计算BFCL分 |
| [τ-bench](https://github.com/sierra-research/tau2-bench) | 多次重复的稳定成功 | 同配置独立重复后才计算pass@k/pass^k，不拼接两版Harness记录 |
| [LongMemEval](https://github.com/xiaowu0162/LongMemEval) | 证据召回与最终回答分开 | 先测已取源码是否进入真实下一请求；相关证据Recall、知识更新/拒答另需金标 |
| [AgentDojo](https://arxiv.org/abs/2406.13352) | 同时关注攻击目标和用户任务 | 攻击成功、正常任务效用分开；普通策略拒绝次数不充当防御成功率 |

## 指标目录及可观测边界

[36项机器可读定义](../benchmarks/system_metrics_v2.json)包含公式、分母、证据、来源和可用性：11项复用已有结果，16项可从既有轨迹新增计算，9项需要进一步金标、同配置重复、攻击对照、标注或时间戳。不是36项都已有有效成绩；零机会的0/0为未评估，缺失不填零。

新计算器为 [system_diagnostics.py](../pico/evaluation/system_diagnostics.py)，新报告入口为 [report_system_comparison.py](../scripts/report_system_comparison.py)。复用既有 ProcessObserver 的版本化源码片段、实际出站请求、工具快照及外部验收，不重新请求模型。

- 最终测试通过比例只表示验收用例覆盖，不等于修复成功或净进展。测试可能原本就通过；没有独立基线逐测试匹配时，不倒推出回归保留率。
- 接口准入率只检查记录到的拒绝/参数错误，不证明工具选得正确。预期修改前失败测试仍是合法调用。
- 同状态重复动作依据工具、参数和完整修改前快照，允许修改后重新测试；重复不必然无用。
- 编辑生效率基于真实文件变化；改变文件不代表修对。补丁纠正需后续同文件实际变化，不以其他文件编辑代替；不完整运行的纠正率保留未知。
- 源码传递率只看有版本和行号的读取片段在下一真实请求是否可见。同请求间隔去重；没有下一请求属于未观察，不算上下文丢失。它不评价必要证据召回、语义摘要或记忆收益。
- 上下文Token峰值、增长比和缓存命中仅使用服务端usage，缓存字段缺失为未知。截断输出占比仅计可核对输出Token，不等于全部重试成本。
- 最终越界检查、常规调用拒绝、真实攻击对抗分开。测试进程隔离不意味着整个Agent宿主进程已隔离。

## 如何呈现系统分数

原 `verified-repair-score-v1` 及所有失败/分母不变。其“修改前实测”等是冻结的流程合同，功能分另列。新诊断不事后塞进原总分；本轮也不按所见结果挑选多维权重。

每批分别给出：完整计划覆盖、全部已启动尝试、双方都已启动的任务交集、双方都可验证成功的效率子集。后两项为明确标注的事后辅助分析，不能覆盖原计划或删除预算失败。不同版本不合并成重复运行；共享额度导致尾部顺序相关，不能把未启动计作模型失败或宣称完整100分。

未来正式运行应先冻结任务、验收、指标目录、系统各自配置及权限/资源条件。可以保留不同原生工具和实现；测试环境存在差异时记录差异和兼容性资格。需要定位某个模块的因果收益时，再额外设计消融，而不是把所有系统横评都要求成单因素实验。

## 已实施与尚未评估

现有47次尝试复核结果见[系统多维报告](../validation/system-comparison-v2-2026-11-07/report.md)，不增加任务或模型调用。Memory更新、相关证据Recall、语义忠实性、攻击成功率、固定配置三次稳定性及TTFT目前不发布成绩。后续可以复用已有ProcessBench和安全资产接入专项评价，但脚本化机制测试不能当作外部模型能力。

```powershell
python -X utf8 scripts/report_system_comparison.py --baseline validation/glm-repair30-baseline-2026-11-06 --compatibility validation/glm-repair12-compat-2026-11-06 --output validation/your-new-system-view
```

逐运行新诊断需要原始证据目录，解压两批 `audit.json` 指向的本地证据包到项目根目录。缺少轨迹时报告 `observed=false`，不会假装得到完整诊断。旧模型成绩、原始记录、参考补丁和API凭据不改动。
