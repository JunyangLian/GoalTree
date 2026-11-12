# 共享记忆采用错误：根因修复与GLM复测（2026-11-12）

沿用原3个场景、每场景12轮，正式复测严格通过 **36/36**；上轮6个失败步骤在本批通过 **6/6**。完整自动化 **985项，失败0、错误0、跳过0**。旧30/36、此前开发批次及失败均保留，不覆盖或追溯补分。

## 问题与实际修复

先前模型把当前Goal代号填入共享资料字段，之后又沿用该错误。条目已被检索和送达，因而不是“未找到记忆”。消息合并对照未解决问题，没有采用该方案。让核对器查看原错误答案，也观察到错误被保留；不能只靠模型评价自己的候选答案。

另一个可见冲突是宿主生成的笔记条件要求“使用前先验证来源”。现在明确区分引用笔记中的未核验说法与把说法用于行动/决策，保留原信任等级、来源和授权。这个文字澄清单独的诊断也未解决所有错误，未将它包装为充分根因。

最终采用[一次只读来源核对](shared-reference-grounding.md)：独立窗口只读当前问题与已授权笔记，不接收原答案及当前Goal的身份值；输出带来源ID和逐字引文。宿主核对引文、结构化值以及显式字段与值对应关系，再按原回复结构保存答案。没有读取金标、硬编码测试代号或按题目匹配替换答案。

实际采取了新的执行步骤，而非证明主模型已学会区分代号。本批记录到 **1次候选答案的语义纠正**；最终源码的适用范围与限制见机制文档。

## 正式36轮结果

| 场景 | 启动/计划 | 严格通过/计划 |
|---|---|---|
| fish-report | 12/12 | 12/12 |
| meeting-plan | 12/12 | 12/12 |
| code-release | 12/12 | 12/12 |

任务描述、金标和验收检查与旧批一致：任务定义一致=True，逐轮提示/金标一致=True。继续使用外部stdlib验证器检查最终字段、实际工具读取、更新验收、独立子Session、人工验收待复核以及其他Goal/Task不被修改。

模型仍是智谱普通API `glm-5.3-flash`，原生Chat Completions，temperature=1、top_p=0.95、Thinking enabled/clear_thinking=false、reasoning_effort=low，输出1536、超时120秒、并发1、传输尝试1、每轮7个工具步骤、截止300秒。输入窗口1,048,576，估算输入上限1,032,192，物理上限32MiB，AppContainer及原审批保留。新增只读阶段使用同模型及预算，额外要求JSON响应；最多每Run一次，不提供工具。

## 核对链路证据

| 场景/步骤 | 读取器结论 | 答案语义被纠正 | 逐字引文来自实际来源 | 未提前流出候选/内部包 |
|---|---|---|---|---|
| code-release/t07 | supported | False | True | True |
| fish-report/t07 | supported | False | True | True |
| meeting-plan/t07 | supported | True | True | True |

核对前后检查同意、来源版本、宿主策略及取消。伪造引文/来源、字段错配、ID前缀借用或可执行返回不被采用；超时与来源撤销测试保留失败和终止状态。候选正文在核对前不展示，网页显示“正在核对记忆来源”；内部协议包不写成聊天答案。源笔记仍未核验，不自动晋升成真相或权限。

读取器返回insufficient时保留原候选并明确记录未评估，不把它标成已核对事实。该机制只覆盖有共享参考、未执行工具且候选不超过12,000字符的原生通道问答；不是所有自由文本回答的强制事实证明。字面引文检查不能完整解决复杂语义和条件适用性。

正式批父工具调用22、子工具调用3；过程/隔离/验收检查全部保持=True。独立读取3次，共1,390 Token，其请求、输入/输出和attempts分别记录，不算tool_steps，也不当作免费调用。原工具权限、ACL、冻结测试和人工验收标准未放宽；本批没有执行Shell或进行新的浏览器实测。

## 全部诊断与费用

| 本轮批次 | 已知请求 | 已知Token | 保守估算元 |
|---|---|---|---|
| formal-v1 | 67 | 482,959 | 0.3933032 |
| independent-reader-diagnostic | 3 | 1,786 | 0.0019788 |
| minimal-reader-diagnostic | 3 | 1,142 | 0.0013076 |
| packing-diagnostic-v2 | 6 | 38,716 | 0.0319968 |
| reader-contract-diagnostic | 3 | 1,966 | 0.0021348 |
| review-diagnostic | 3 | 2,166 | 0.0027628 |
| runtime-probe | 7 | 32,331 | 0.0274408 |
| typed-reader-diagnostic | 3 | 1,264 | 0.0014032 |
| use-contract-diagnostic | 3 | 19,472 | 0.0161376 |

正式Agent流程与原始公共请求回放是不同实验，不将回放和重复运行当独立任务或合并成功率。诊断中无效JSON、insufficient和错误保留答案都留在本地原始文件。首次packing-diagnostic因脚本调用了错误的本地配置方法失败，模型请求0次；修正后另用新目录。runtime-probe在补充超时单测后触发源码冻结检查而停止，6轮启动、5轮通过、后3轮未启动；这批不算完整验收，全部证据保留。

本轮共98次已知真实请求、**581,802 Token**，未知用量预留0，按[智谱官方单价](https://docs.bigmodel.cn/cn/guide/start/pricing.md)保守估算 **¥0.4784656**，实际账单未知。所有输入按未命中缓存估算，未知不记零。本轮80万Token/估算2元上限符合=True；旧轮账本及未知请求预留原样保留。

## 复现与边界

这是3个已知合成开发流程的受控回归，未增加未见留出任务。一次完整复测不代表普遍100%记忆准确率或稳定性；多轮方案同时增加了独立读取和宿主检查，不能把变化当成单因素模型能力提升。仍没有新增百万Token复杂任务泛化、真实搜索/PDF全链路或生产隐私泄漏率结论。

全量测试耗时1265.62秒，冻结334个源码文件，运行期变化0。正式批快照不匹配0，当前源码不匹配0。每请求必保内容、预算、只读工具缺席、原候选未进入读取器及JSON模式由独立审计逐条复验。真实Key精确扫描命中0。

本地证据目录：`artifacts/memory-grounding-2026-11-12/`。

- `formal-v1/freeze.json`、`source-snapshot/`、`fixtures.json`：首次请求前固定源码、任务及验收。
- `formal-v1/*/t*/requests/`、`responses/`、`result.json`：主模型与来源读取器请求、用量、回答、实际状态/工具与审批。
- `formal-v1/verification.json`、`turns.csv`：原外部验证器的严格结果。
- 各工作区`evidence.sqlite3`中的`shared_reference_grounding`：逐字来源、版本与候选/采用答案哈希；`audit.json`导出独立对照。
- `runtime.diff`：从上轮固定源码到当前的运行时差异。
- `full-validation/full-tests.xml`、`full-tests.log`、`summary.json`：最终完整回归及源码冻结检查。
- 其余诊断目录、ledger.jsonl、冻结检查中止的runtime-probe：保留全部候选、失败和费用。

```powershell
python -X utf8 -m pytest tests/test_memory_grounding.py tests/test_shared_memory.py tests/test_web_status.py -q
python -X utf8 scripts/verify_long_workflows.py --output artifacts/memory-grounding-2026-11-12/formal-v1
python -X utf8 artifacts/memory-grounding-2026-11-12/audit.py
```

上述独立核验不调用外部模型。正式收费执行入口仍是`eval_long_workflows_live.py --live`，输出目录必须新建，重新执行前需核对累计预算。原始会话及隐私控制文件默认本地保留，不自动上传。
