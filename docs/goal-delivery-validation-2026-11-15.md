# 交付契约与目标推进修复验证（2026-11-15）

全量自动化 **1079项通过**（失败0、错误0、跳过0）。同一组3个合成场景、15轮连续真实GLM对话，本批严格通过 **12/15（80.00%）**，五轮全部通过的场景 **1/3**。这些是已参与诊断的开发任务，不能作为未见任务泛化或公开Benchmark成绩。

## 问题和改动

1. **通用任务仍受代码修复角色影响。** 宿主根据绑定选择协调器、通用子执行器或原CLI代码角色；通用角色保留当前Task、祖先约束、依赖及原验证要求，不把资料核查强行变成代码修复。
2. **交付规格与源文件事实混淆。** 新增可选Task `output_contract`，明确artifact/evidence/execution、交付形式及要求出现的节点字段。当前请求或同Session真实读取证据提供逐字引文，宿主核对引用、当前文件SHA和路径授权并生成来源记录。保存与修改时拒绝遗漏已声明名称的字段；模型不能静默清空、降级或削弱契约。它进入Goal上下文、独立Task和handoff，原验收和权限不变。
3. **JSON末尾附言。** 仅针对当前请求中有限、明确的JSON布局指令，校验最终答复语法与末尾位置，最多两次纠正；所有调用入账，不填金标字段、不更改事实。未知语义要求不宣称已被通用解析。
4. **核查结束但没有交付正文。** 明确区分核查结果、获准撰写的待复核草稿、正式执行与最终验收。沿用record_progress提交实际草稿，不删依赖、不自我验收、不绕过run_task或文件/Shell审批。先提交再改规格时仍必须重新核对当前产出。

新增61项机制/回归测试计入全量总数，其中交付契约25项、布局/角色等36项。覆盖伪造引用、跨Session证据、撤权/过期哈希、缺失字段的原子拒绝、旧产出失效、恢复后契约保持、有限纠正及原审批依赖；不把针对性重复执行相加。

## 保留失败的批次对照

| 批次 | 启动/计划轮次 | 严格通过/计划轮次 | 全流程场景 | 真实模型请求 | 已知Token |
|---|---:|---:|---:|---:|---:|
| 此前工程修复 | 15/15 | 6/15 | 0/3 | 77 | 635,823 |
| 通用角色、提示与JSON布局修复 | 15/15 | 3/15 | 0/3 | 77 | 756,390 |
| 交付契约，主机睡眠中断 | 9/15 | 8/15 | 1/3 | 46 | 415,627 |
| 同代码完整复跑，临时阻止自动待机 | 15/15 | 12/15 | 1/3 | 74 | 721,591 |

中间批次3/15的失败主要是format仍报missing、格式名被改写、字段遗漏及未提交当前规格产出。该批完整保留，没有挑选或替换失败。前后都只有一个模型样本，Harness发生改变，部分随机选择与工具开销不同；不把差值解释成单因素因果提升或稳定成功率。

交付契约首批在第9轮被主机睡眠中断：Windows Power-Troubleshooter记录SleepTime=2026-11-15T12:03:20.8557110Z、WakeTime=2026-11-15T12:48:28.9278036Z，与2751.969秒的取消请求重合。该请求没有usage，按原规则保留37,180 Token和¥0.03384未知预留并停止后续调用；首批8/15通过、9/15启动、1/3完整场景，6轮未执行。不把这个时间归因于服务端慢，也不填造usage。

随后在完全相同源码/协议下做一批完整复制，使用临时SetThreadExecutionState仅抑制自动空闲待机，未修改系统持久电源设置、显示器状态或手动睡眠。中断批不恢复、不覆盖；完整复制与原批分别报告。动态运行环境已改变，不能将其时间差解释为模型/Harness效率提升。

本批逐场景结果：

| 场景 | 严格通过轮次 |
|---|---:|
| library-brief | 4/5 |
| garden-review | 5/5 |
| archive-handover | 3/5 |

## 冻结与复现

首次请求前冻结344个源码/测试/评测资产、案例、公开资料、15轮提示、金标及协议。独立审计确认四批案例、提示/金标、公开文件、资产SHA和模型运行协议相同；本批全量测试与模型评测的源码冻结相同且运行期间未变。原验证器、验收金标及工具权限没有改动。

普通智谱API `glm-5.3-flash`，temperature=1、top_p=.95、Thinking enabled/low、clear_thinking=false；输出2048 Token、每请求120秒、传输1次、每轮共享14工具步、并发1、80请求/120万Token/估算2元批次上限。原AppContainer、只读任务、人工待验收与审批保持。服务商未提供不可变权重修订，模型别名并非权重版本证明。

```powershell
python -X utf8 -m pytest -q --junitxml=artifacts/my-full-tests.xml
python -X utf8 scripts/eval_goal_autonomy.py --output artifacts/my-goal-diagnostic --max-total-tokens 1200000 --max-spend-cny 2 --live
python -X utf8 scripts/verify_goal_autonomy.py --output artifacts/my-goal-diagnostic
```

每次使用不存在的新输出目录；普通API Key仅从本地配置读取。首次复现会冻结当时实现，保留原请求、响应、工具事件、审批、状态、用户文件哈希和独立验证结果。不把测试金标或参考答案交给Agent。

## 过程检查和开销

| 指标 | 结果/适用分母 |
|---|---:|
| 初始计划交付形式同时进入三个字段 | 2/3 |
| 需求变更后标题/描述/验收同步 | 3/3 / 3/3 / 3/3 |
| format字段与当前交付契约一致 | 7/9 |
| 独立子Session读取v1 | 3/3 |
| 刷新后实际读取v2且哈希一致 | 3/3 |
| 刷新轮提交当前规格的交付物 | 3/3 |
| 原Task保留、没有重复建树 | 6/6 |
| 静止服务重构状态保持 | 3/3 |
| 外部Goal/Task状态不变 | 15/15 / 15/15 |
| 用户文件只读 | 15/15 |
| 没有自行验收 | 15/15 |

本批74次模型请求、721,591已知Token、0次usage未知、0次原生协议错误；工具调用64次（父转发子事件只计一次）、失败工具调用8次。成功读取22次，同actor/同Run/同范围/同哈希重复读取0次；这个口径不衡量跨会话冗余，不能替代Recall或说明检索提升。JSON布局拦截0次，与原生工具协议错误区分。

本批按冻结费率估算¥0.6142748，不是账户实际扣款；连同本轮保留的中间和中断批次共197次请求、1,893,608已知Token、已知估算¥1.5991264，另保留未知费用¥0.03384。未知预留本批¥0；中断请求37,180 Token只是保守预留，不是真实usage，未知Token不按零计算。此前6/15批次另计，不与本轮费用重复相加。

## 失败、过程错误和限制

| 场景/轮次 | 未通过的原冻结检查 |
|---|---|
| library-brief/t01 | delivery_contract |
| archive-handover/t02 | field:format |
| archive-handover/t04 | field:format |

初始规划实际声明结构化artifact契约的场景为1/3；其余仍沿用兼容的自由文本Task。机制能够约束已声明契约，不代表模型每次都会声明。不能把所有通过都归因于结构化契约，更不能以用户可见答复声称“字段已写明”代替检查持久化任务内容。尚未声明时仍依赖模型正确同步字段，这是明确的剩余风险。

本批过程错误（不因最终检查通过而隐去）：

| 场景/轮次 | 工具 | 状态 | 原始错误摘要 |
|---|---|---|---|
| library-brief/t04 | goal_action | rejected | error: invalid arguments for goal_action: Invalid \escape: line 1 column 789 (char 788) |
| library-brief/t04 | goal_action | error | error: tool goal_action failed: 待验收需提供output或当前会话evidence_id之一。 |
| garden-review/t02 | goal_action | error | error: tool goal_action failed: 待验收引用必须是产出证据，不能把搜索片段等同于已完成工作。 |
| garden-review/t04 | goal_action | error | error: tool goal_action failed: 待验收需提供output或当前会话evidence_id之一。 |
| garden-review/t04 | goal_action | error | error: tool goal_action failed: 待验收引用必须是产出证据，不能把搜索片段等同于已完成工作。 |
| archive-handover/t01 | plan_tasks | rejected | error: invalid arguments for plan_tasks: Output contract quote is not in the actual current request/read evidence; preserve the literal user label |
| archive-handover/t01 | plan_tasks | rejected | error: invalid arguments for plan_tasks: Output contract quote is not in the actual current request/read evidence; preserve the literal user label |
| archive-handover/t01 | plan_tasks | rejected | error: invalid arguments for plan_tasks: Committed output format '移交长文' must appear literally in title. Repair the requested fields; do not silently rename or discard the contract. |

完整工具错误、审批请求及恢复事件保存在逐运行result.json；不能因最终通过就删除中间失败。confirm_task只是请求人工批准，被拒绝不表示绕过成功；冻结严格检查仍原样计分。

本批只有3个合成场景，每场景连续5轮，不是15道独立任务；它们已经参与调试，不是留出集。契约引用证明观察过原文，不证明语义理解完备。外部验证核对最终字段、真实读取、当前规格产出和边界，未评价任意长文质量。人工待复核不等于最终已验收。此处服务重载为静止对象重构，不是崩溃恢复；上下文规模也不代表1M复杂长任务。未执行浏览器验收，不宣称网页已重新验证。

具体剩余问题：图书馆首轮计划漏把交付形式写入真实标题，但最终回复声称已写入；档案的核查与刷新轮将format填为源文件扩展名md，即使当前artifact契约已经明确。两类问题分别说明可选声明仍会遗漏，以及存在契约不等于模型一定忠实使用。原提示的format字段未用机器可读映射区分文件格式与交付形式，未来新协议可预先明确二者，但本批不追改提示、金标或分数。独立二次复验与主验证结果一致。

## 证据路径

- 当前完整复制批次：`artifacts/goal-delivery-2026-11-15/awake-v1/`，含freeze、源码快照、逐轮requests/responses/result、ledger、verification.json/CSV、evidence-sha256。
- 睡眠中断批次：`artifacts/goal-delivery-2026-11-15/contract-v1/`；系统睡眠证据与临时抑制记录为同层`power-events.json`、`keep-awake-start.json`和结束记录。
- 全量：`artifacts/goal-delivery-2026-11-15/full-validation/`，含同源冻结、pytest完整日志、JUnit XML及退出状态。
- 独立审计：`artifacts/goal-delivery-2026-11-15/audit.json`，逐项检查冻结一致性、计费记录及源文件凭据扫描。
- 中间失败：`artifacts/goal-delivery-2026-11-14/formal-v1/`；此前结果：`artifacts/goal-autonomy-2026-11-14/`，全部保留。
- 可公开汇总：[逐轮检查与冻结哈希](goal-delivery-checks-2026-11-15.json)、[逐轮CSV](goal-delivery-runs-2026-11-15.csv)、[机制与限制](goal-delivery-reliability.md)。原始轨迹含本机目录默认只在本地保留，不自动发布聊天记录或凭据。
