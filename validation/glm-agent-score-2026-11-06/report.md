# CodeTrail / ApeCode：GLM 可验证修复分与兼容性复测

实验于2026-11-06运行，2026-11-07汇总。两批均为真实普通 API 调用，使用 `glm-5.3-flash`、真实工具与 AppContainer 外部验收。项目实现、修正、数据整理由 Codex 完成；GLM 仅用于被评测 Agent 的修复决策和必要接口预检，没有用于项目开发、生成金标或充当主观裁判。

> **运行后审计更正（2026-11-07）：发现 CodeTrail 工具回调绑定缺陷。** 两批的 Agent 内部测试并未全部使用声明的固定3.10运行时：CodeTrail 工具注册时已捕获原3.13 AppContainer回调，事后赋值没有替换该闭包；ApeCode及两次独立外验使用固定3.10。下方分数保留为历史观察值，**不作为符合统一运行时协议的正式横评成绩或排名**。最终文件的两次独立验收仍成立；内部流程/成本受执行环境差异影响。新代码修复只做离线验证，预算内不再补跑或替换历史结果。详见[运行时一致性更正](runtime-conformance-erratum.md)。

## 历史观察分及覆盖（运行时不完全一致）

主分为四类任务等权的 **可验证修复率**：每次必须代码通过冻结外验、改动范围合法、Agent 正常结束并实际先测/最后修改后重测、第二次独立验收通过。另列只要求合法改动及功能验收通过的功能分。Token、速度、计划文字不参与加分。规则在首次正式调用前冻结，见[计分定义](../../docs/agent-score-v1.md)。

| 批次 | Agent | 已记录/固定核心槽位 | 功能分 | 可验证修复分 | 核心主分上下界 |
|---|---|---:|---:|---:|---|
| 初始基线 | codetrail | 12/12 | 83.33 | 83.33 | 83.33–83.33 |
| 初始基线 | apecode | 12/12 | 83.33 | 83.33 | 83.33–83.33 |
| 兼容性复测 | codetrail | 11/12 | 未完成（无单点分） | 未完成（无单点分） | 91.67–100.00 |
| 兼容性复测 | apecode | 12/12 | 66.67 | 50.00 | 50.00–50.00 |

两批共有 **12 道不同任务、47 次实际尝试**（初始 24，复测 23），没有把复测算成新题。最初全30题计划60次，最后一个核心 worker 触发645秒截止，按冻结规则停止，余36槽位未启动；原全30题主分仍为未完成，没有换成核心分冒充全量分。

核心题为四类各3道；其中6道是上游故障的最小复现，1道为真实上游故障任务，5道自设计。全30题来源则为18道上游故障（含最小复现）、12道自设计。题目通常规模较小，不能当作全仓库复杂任务。所有题都已被历史评测或开发接触，源30题中6道标注曾额外调优，其中核心12题内为 inventory_reservation、boltons_xfrange、boltons_splitlines、humanize_metric 四题；历史 `holdout_` ID 不能证明本轮未见。

**不能据此宣布泛化排名或因果提升。** 每题每批仅一次，兼容性复测不是独立新样本，也不是预先三次稳定性实验。没有稳定分、置信优越性或 SWE-bench 官方分。

## 逐题结果

表中为“功能通过 / 可验证通过”；未启动保留未知。所有已启动失败进入分母。

| 任务 | 类别 | 来源 | 历史额外调优 | 初始 CT | 初始 Ape | 复测 CT | 复测 Ape |
|---|---|---|---|---|---|---|---|
| money_minor_units | cross_file | author_created | 否 | 1/1 | 1/1 | 1/1 | 1/0 |
| inventory_reservation | cross_file | author_created | 是 | 1/1 | 1/1 | 1/1 | 1/1 |
| holdout_dotenv_override | cross_file | real_upstream_bug | 否 | 0/0 | 1/1 | 1/1 | 0/0 |
| pagination_boundaries | single_file | author_created | 否 | 1/1 | 1/1 | 1/1 | 1/1 |
| ttl_expiry | single_file | author_created | 否 | 1/1 | 1/1 | 1/1 | 1/0 |
| boltons_splitlines | unknown_location | upstream_minimal_reproduction | 是 | 1/1 | 1/1 | 1/1 | 0/0 |
| retry_schedule | single_file | author_created | 否 | 1/1 | 1/1 | 1/1 | 1/1 |
| humanize_fractional | regression_compatibility | upstream_minimal_reproduction | 否 | 1/1 | 1/1 | 1/1 | 1/1 |
| boltons_xfrange | unknown_location | upstream_minimal_reproduction | 是 | 1/1 | 0/0 | 1/1 | 0/0 |
| boltons_isoparse | unknown_location | upstream_minimal_reproduction | 否 | 1/1 | 1/1 | 1/1 | 1/1 |
| humanize_list | regression_compatibility | upstream_minimal_reproduction | 否 | 1/1 | 1/1 | 1/1 | 1/1 |
| humanize_metric | regression_compatibility | upstream_minimal_reproduction | 是 | 0/0 | 0/0 | 未启动 | 0/0 |

## 失败与恢复

首批四次失败全部保留：CodeTrail 的 dotenv 任务在20步边界出现未送达原生工具结果，humanize_metric 连续本地字段拒绝/截断后触发 worker 截止；ApeCode 的 xfrange 和 metric 因4096输出预算截断结束。终止不是单个API180秒超时。终止后另行无模型外验，确认 metric 工作区未改且测试仍失败；没有给终止任务补跑模型或替换结果。

本次只修正两系统共同的一个本地协议假设：已有 `reasoning_content` 字符串原样保留；字段缺失/null 时不按正数或未知 reasoning_tokens 强求重发，不虚构思考；畸形类型、截断动作、错误模型、非法参数和原权限校验保持。正数用量不证明客户端获得了对应正文，因此旧拒绝不能直接定性为服务端违反协议。参见[兼容说明](../../docs/glm-compatibility-retest.md)。

修正不包括修改修复任务金标、放开Shell或提高输出预算。复测再度出现的截断/流程失败照实保留。协议拒绝变少本身受本地规则变化影响；更直接的证据是实际调用继续执行、工具结果回传、已有思考原样回传及独立验收。

| 复测失败运行 | 功能/可验证 | 归因 |
|---|---|---|
| 002-apecode-money_minor_units | 1/0 | workflow_initial_test_missing |
| 006-apecode-holdout_dotenv_override | 0/0 | model_output_budget_truncation |
| 010-apecode-ttl_expiry | 1/0 | workflow_initial_and_final_tests_missing（前后测试命令均被拒绝） |
| 011-apecode-boltons_splitlines | 0/0 | model_output_budget_truncation |
| 018-apecode-boltons_xfrange | 0/0 | model_output_budget_truncation |
| 023-apecode-humanize_metric | 0/0 | budget_pretransmission_stop（共享预算拦截） |

第023次已经启动并产生4个工具记录，但下一请求在发送前触发Token预算守卫，记为本资源条件下的失败，不称为模型无法修复；第024次尚未启动，保持未知。原标准化数据保留粗分类 `model_or_controller_error`，本报告据原始 `BudgetExceeded` 异常细化归因为 `budget_pretransmission_stop`，见 [补充归因](failure-attribution.json)。因运行顺序与资源消耗存在关联，不能用这两种结果评价纯模型能力。

案例：复测 ApeCode 的 money_minor_units 给测试命令添加 `2>&1` 等内容而被白名单拒绝，后来代码通过外验及修改后测试，但没有取得修改前基线，所以功能通过、流程不通过；其最终回答如实承认未测基线，不能称虚假成功声明。CodeTrail 的 dotenv 复测通过两次独立外验，仍保留19步及两次截断重试的实际开销。

## 过程及成本

| 批次 | Agent | 已观测工具步（含失败） | 协议拒绝及原因 | 所有已知输入+输出Token | 用量是否完整 |
|---|---|---:|---|---:|---|
| 初始基线 | codetrail | 95 | {'truncated': 9, 'missing_reasoning': 8} | 546959 | False |
| 初始基线 | apecode | 105 | {'truncated': 2} | 465487 | True |
| 兼容性复测 | codetrail | 85 | {'truncated': 8} | 517377 | True |
| 兼容性复测 | apecode | 113 | {'truncated': 3} | 370255 | True |

**共享预算窗口累计**（包含首批、复测、预检与失败）：已观测 1,902,634 Token，未确认/未知请求另保守预留 66,108 Token；标价上界估算 1.9387952 元，另预留 0.0692704 元。下一请求需要31,745 Token保守预留：1,902,634＋66,108＋31,745＝2,000,487，超过2M上限487，发送前被拒；5元金额上限未触发。当前上限2,000,000 Token与5元，先到停止。账户可用20M额度不等于本轮授权20M消耗。未知用量没有填零；实际平台扣款未知，输入按未命中缓存上界计价。复测summary的账本已经含首批，不能把两份累计账本相加。

单位成功成本必须含全部失败成本；有未知使用量时真实单位成本仍未知。首批CodeTrail已知546,959 Token包含终止任务的127,235已知Token，而不是初版遗漏后的419,724。初版导出与纠正说明均保留，主分和原始数据不变。

正常运行耗时从Agent执行到第一次外验结束；首批终止运行645.642秒为调度到强制结束，包含初始化。两种时钟分开保存。两方共同成功题的配对p50/p95见各批 diagnostics.json，不以失败早退的速度作为优势；小样本p95不是线上SLA。

重复读取按冻结指标与源码行重叠诊断分别保留：首批dotenv精确区间重复计数为0/13，但340/781条归属源码行是同文件版本的重复返回。行重叠是事后诊断，不等于无效读取，不纳入主分，不能代替Recall或记忆质量。

复测原生序列的逐call-ID及思考字符串审计是事后观察（包含被拒绝调用的结果返回，不等于所有工具执行成功）：

| Agent | 非截断原生工具响应 | 下一请求可见 | 已有思考原样回传/可核对 | 缺失思考且实际回传工具结果 | 随后观察到服务端响应 |
|---|---:|---:|---:|---:|---:|
| codetrail | 65 | 65 | 49/49 | 16 | 65 |
| apecode | 74 | 73 | 62/62 | 11 | 73 |

## 控制条件与可比性边界

- 两批选题、题面、初始文件、验收测试、模型参数、上游ApeCode源码、声明的Python及权限配置一致，controlled-checks.json确认的是配置/文件一致性，未证明所有工具实际执行到了指定回调；运行后发现的绑定缺陷另行更正。新源码及协议在首次正式复测前冻结，新结果没有覆盖旧结果。
- 平台响应只给出模型ID，没有可独立锁定的权重快照；本轮固定服务商、ID和请求参数，原始响应完整保留。普通API固定模型ID；Thinking enabled、clear_thinking=false、reasoning_effort=max，temperature=1、top_p=.95、max_tokens=4096，单请求180秒、最多30请求/20工具步、worker645秒、并发1。没有改成套餐端点或伪装客户端。
- 两方保留各自循环、提示与工具粒度，CodeTrail可以重试截断，ApeCode适配器遇截断抛错退出。CodeTrail工具schema列出允许命令，ApeCode原schema未列同样枚举，题面给出精确命令。这些是系统和约束适应差异，不能当上游默认CLI排名或单因素算法比较。
- ApeCode工具测试及两方独立外验使用固定哈希Python3.10.1；Agent循环为3.13.11，CodeTrail内部测试因捕获旧回调仍使用3.13.11，不能称两方内部测试运行时统一。初始3.13参考任务也失败，定位到CPython临时目录0o700能力SID问题；正式前更换受控测试解释器并对全部30题取得30个故障失败/30个参考通过资格。没有修改ACL、冻结金标或退回宿主测试。
- 可信宿主启动器只启动真实Windows AppContainer；每次要求实际内核TokenIsAppContainer证明、无网络能力、固定命令、过滤副本及独立外验。通用宿主Python并未获得任意Shell权限。本轮没有新的攻击集，不能由普通修复任务无越界推断安全满分。
- 输入工作区的验收测试可读且禁止修改；外验从冻结原版复制，不是隐藏测试集，最终正确性仅指这些冻结测试的覆盖范围。原有必要证据代理金标不足以可靠评价Recall、跨会话记忆和语义忠实性，这些指标为未评估。保存原始请求/工具证据，但不补虚构过程分。没有用GLM做自动主观裁判。
- 首批全量自动化466/466；协议兼容修正后471/471，二者有重叠，不相加成937项。相关修正回归53/53。冻结金标哈希保持不变。
- 当前Windows没有可用Linux Docker/WSL发行版，SWE-bench官方公开线未运行；ProcessBench离线资格与真实Agent分数分开。旧GLM两题试验、DeepSeek历史、七维旧评测保持各自口径，不混合成新总分。

核验61份指定桥接执行器的真实内核审计（含ApeCode工具及外验）、23份二次验收结果和旧证据包哈希，见[重算审计](recomputation-audit.json)。这61份记录不能证明CodeTrail的其他内部工具也调用了指定桥接器；回调缺陷因此单独披露，未用内核隔离标记替代运行时一致性证据。

## 修正后验证（2026-11-07）

最终冻结当前源码后，完整自动化 **474/474通过**（691.20秒，无失败/错误/跳过），结束时全部源码哈希仍与运行前一致。[最终JUnit](glm-callback-final-full-v2-2026-11-07.xml) · [日志](glm-callback-final-full-v2-2026-11-07.log) · [源码及测试哈希](glm-callback-final-validation-freeze-2026-11-07.json) · [测试历史](test-history.json)。早先一次全套运行中途改动了源码，仅保留为诊断；不代替最终固定版本全量结果，不把重叠测试数相加。

除GLM思考正文兼容和回调绑定外，修正报告读取JSONL的分隔方式：Unicode U+2028/U+2029可以出现在合法JSON字符串中，必须按LF分记录，不能用splitlines()拆断；真正损坏的记录仍严格报错，没有丢弃坏行来美化统计。3项新增报告回归覆盖这一问题及无证据不声称工具回传。

[真实运行时探针](runtime-qualification.json)由离线脚本决策，Python3.13 Agent实际经过指定Python3.10桥接和AppContainer测试，内核证明及临时目录写入通过；它不是Agent自主修复分数。46份完整worker结果的初始哈希、46个不同会话ID及所有只读文件还经过[工作区审计](workspace-audit.json)；控制器终止的一次缺失完整会话结果，另保留终止后外验，未补造缺失证据。

**修正绑定后的真实模型横评没有运行。** 本轮预算已在旧复测中触发保护，旧47次记录不删除或重标为新版本；后续须重新冻结统一实际运行时的完整对照。输出截断和首批20步预算边界的未送达工具结果也仍是已记录的限制，不以本次局部修正宣称全部根因消失。

复现当前全量与关键回归（不调用模型）：

```powershell
python -X utf8 -m pytest -q --junitxml=artifacts/your-full-suite.xml
python -X utf8 -m pytest tests/test_process_controller.py tests/test_glm_report.py tests/test_glm_live_pilot.py tests/test_benchmark_score.py -q
```

## 交付和复现

- [初始批次完整报告](../glm-repair30-baseline-2026-11-06/report.md)、[初始逐运行数据](../glm-repair30-baseline-2026-11-06/runs-detailed.json)、[资源导出纠正及原版](../glm-repair30-baseline-2026-11-06/reporting-correction.json)。
- [兼容复测报告](../glm-repair12-compat-2026-11-06/report.md)、[逐运行CSV](../glm-repair12-compat-2026-11-06/runs-detailed.csv)、[逐运行JSON](../glm-repair12-compat-2026-11-06/runs-detailed.json)、[过程和来源统计](../glm-repair12-compat-2026-11-06/diagnostics.json)。
- [复测471项JUnit](../glm-repair12-compat-2026-11-06/glm-compatibility-full-2026-11-06.xml)。原始请求、响应、工具、diff、最终文件、两次外验及内核隔离审计的本地zip位置和SHA256见两批audit.json；完整本地证据不冒充远程已上传附件。
- 原始参考补丁不发布、不进入Agent工作区；凭据仅临时进程持有，没有落盘或提交。未推送GitHub，未上传Hugging Face。

复现：先按[方法和准备命令](../../docs/glm-compatibility-retest.md)在新输出目录冻结；设置本地普通API环境变量后运行控制器。重现旧版本须检出对应源码或证据包source.zip，不能使用新代码覆盖旧freeze。重算本地报告不请求模型：

```powershell
python -X utf8 scripts/report_glm_benchmark.py --batch artifacts/your-terminal-batch --output validation/your-new-report --test-report artifacts/your-full-tests.xml
```

本轮保留同一外部验收器确认的最终代码结果和历史流程观察值；由于内部测试运行时不符合统一声明，不能给出合格的同模型横评排名。未见泛化、公开基准和长期记忆收益也仍需要独立实验。
