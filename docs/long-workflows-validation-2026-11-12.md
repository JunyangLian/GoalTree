# GoalTree 批量长流程诊断与修复报告（2026-11-12）

最终固定版本完成3个场景、36轮真实GLM工作流，严格验收 **30/36**；最终完整自动化 **960项，失败0、错误0、跳过0**，退出码0。开发中所有批次、预算中止及模型失败保留。该结果是已知合成流程的开发回归，不能当作公开Benchmark或未见任务泛化成绩。

## 实际测试范围

三个场景分别是鱼类组学公开报告、会议准备、小型工具发布；每个12步、两个独立Goal，批内保留历史，批间全新工作区/会话。实际模型执行新增Task、更新验收、读取资料、缺失文件后恢复、文件版本刷新及run_task分派。共享开启后查询背景、关闭后利用本对话历史，检查约束与人工验收状态是否串扰。Goal切换由宿主选择正确聊天绑定，不声称自动跨Goal路由。

另增宿主长序列：每主题20次依赖/交接变更、60次读取/重启、60组原生协议压力及20次来源变更/撤销循环，以及24轮历史预览。它们是离线机制检查，主题重复与循环次数不算独立任务。新增数据资产、执行器、外部验证器和[冻结方法](long-workflows-method.md)可复用。

真实会话使用1M窗口配置，但本批实际窗口远小于1M。压缩通过固定小字节预算单独施压；**没有证明百万Token复杂长任务可靠性，也没有真实用户长期随访**。搜索、PDF、插件、隐私、代码补丁/测试与运行恢复等由全量自动化回归覆盖；本批未额外把这些服务全部接入真实模型场景，未进行新的真实浏览器验收或恶意Shell执行。

## 已修复的工程问题

1. **历史时间顺序**：此前先拼接所有旧用户要求，再拼接旧答案，破坏真实顺序。现在按Session内真实序号交错排列，仍标为历史数据，不能成为新指令、验收或授权。
2. **预览静默截断**：历史答案只显示600字符却无省略提示。现在记录preview_truncated、返回/总字符数和同Session证据ID，缺失细节可用recall_evidence读取，不把预览当完整答案。
3. **当前问题与新背景的位置**：内部保留固定受保护帧用于压缩；实际网络请求按历史→当前宿主状态/规则及检索背景→唯一当前问题→当前执行交换排序。输出审计提供实际索引，复核必保内容SHA256及预算。纯工具历史保持原始结构，不因压缩摘要被当成聊天历史而移动。
4. **重复旧要求及互联冲突**：旧用户要求已在历史证据里，仍被复制到当前请求的历史目标摘要，造成旧限制被误认为当前要求。Workbench停止重复该字段；工作区提示明确允许引用已授权检索背景，同时不允许修改来源Goal、继承其约束或当作验证证据。
5. **身份与类型混淆的约束说明**：区分当前Goal身份和被询问背景，保留源版本字符串、字面标识及错误码，普通预算更正避免无关读取。这是通用上下文说明，不做按题目匹配的答案替换，也不提升背景信任级别。
6. **评测统计纠正**：父流程会转发子任务事件，统计按真实执行者去重；子任务首次读取不当作父会话重复。缺失文件错误码在收费运行前用真实工具离线核对，保持invalid_arguments金标；隐私核心降级被拒绝属于保护生效，夹具改为预期拒绝。

## 全部模型批次

| 批次 | 已启动/计划轮数 | 严格通过/计划 | 真实已知请求 | Token | 保守估算元 |
|---|---|---|---|---|---|
| live-v1 | 33/36 | 30/36 | 61 | 455,648 | 0.3713564 |
| live-v2 | 36/36 | 32/36 | 63 | 471,211 | 0.3834008 |
| live-v3 | 36/36 | 32/36 | 64 | 479,077 | 0.3886676 |
| memory-followup-v1 | 9/9 | 9/9 | 9 | 56,611 | 0.0470488 |
| live-v4 | 36/36 | 30/36 | 64 | 479,562 | 0.3895476 |

live-v1固定50万Token，实际455,648后下一请求保守预留超限；1次请求在发送前拒绝，最后3轮未启动。被拒绝的请求不当作已调用模型；已完成子任务仍保留。live-v2及以后完整批次各自固定60万Token/1元上限，memory-followup-v1固定10万Token/0.2元。用户声明有3000万额度，本轮未尝试耗尽该额度。各批都有独立账本，不把新目录当免费重置旧用量。

live-v2及live-v3都为32/36，但失败场景不完全相同；9/9短定向测试未预测后续完整流程中的共享采用失败。因此不能用短测试替代长流程结果。最终live-v4是在明确修复后开展的一次完整开发回归；所有批次都列出，不合并成单一漂亮分数，不筛选失败轮次。没有未见留出集，也没有同版本多次重复可支持稳定性结论。

最终逐场景：

| 场景 | 启动/计划 | 严格通过/计划 |
|---|---|---|
| fish-report | 12/12 | 10/12 |
| meeting-plan | 12/12 | 10/12 |
| code-release | 12/12 | 10/12 |

## 过程、隔离及效率

最终观察到协议错误0；未知用量请求0。其他Goal和Task状态保持检查为True。子任务有独立会话、同Goal规范绑定、真实读取、未改文件、人工验收仍待复核；逐项布尔检查见机器报告。它们不代表全部OS权限/出网攻击测试通过，本批虽保留AppContainer配置，但无真实Shell执行。

最终父工具事件21、子工具事件5，去重共26。父会话同路径同哈希重复读取3次，扣除t06明确要求再次读取后剩0次；子会话首次读取单列。读取字节/行范围的完整价值未单独打分，不将该小样本读次数直接解释成检索召回率提高。

最终请求必保哈希不匹配0，超原预算0。输入/输出Token来自服务回执，最终模型请求耗时p50=4.078秒、p95（nearest rank）=7.797秒，耗时总和300.266秒；这不是单个任务的用户端延迟。本机自动化并行运行，不据此作纯服务速度比较。

本轮全部新增模型批次共 **1,942,109 Token**，未知预留0，保守费用约 **¥1.5800212**。按[智谱官方定价](https://docs.bigmodel.cn/cn/guide/start/pricing.md)输入0.8元/百万、输出2.8元/百万，全部输入按未命中缓存估算；实际账单未知。之前轮次未知用量预留不删除、不记零，单独在原账本保留。

## 原始失败清单

| 批次 | 场景/步骤 | 状态 | 未通过项 |
|---|---|---|---|
| live-v1 | code-release/t07 | completed | field:shared_code |
| live-v1 | code-release/t08 | completed | field:file_version |
| live-v1 | code-release/t09 | failed | completed |
| live-v1 | code-release/t10 | unstarted | unstarted |
| live-v1 | code-release/t11 | unstarted | unstarted |
| live-v1 | code-release/t12 | unstarted | unstarted |
| live-v2 | meeting-plan/t07 | completed | field:shared_code |
| live-v2 | meeting-plan/t10 | completed | field:shared_code |
| live-v2 | code-release/t07 | completed | field:shared_code |
| live-v2 | code-release/t10 | completed | field:shared_code |
| live-v3 | fish-report/t07 | completed | field:shared_code |
| live-v3 | fish-report/t10 | completed | field:shared_code |
| live-v3 | meeting-plan/t07 | completed | field:shared_code |
| live-v3 | meeting-plan/t10 | completed | field:shared_code |
| live-v4 | fish-report/t07 | completed | field:shared_code |
| live-v4 | fish-report/t10 | completed | field:shared_code |
| live-v4 | meeting-plan/t07 | completed | field:shared_code |
| live-v4 | meeting-plan/t10 | completed | field:shared_code |
| live-v4 | code-release/t07 | completed | field:shared_code |
| live-v4 | code-release/t10 | completed | field:shared_code |

共享错误既可能直接忽略背景，也可能把当前Goal代号填成共享代号，随后沿用错误答案；后续失败不应重复解释成多个完全独立根因。JSON字段类型/字面值不匹配照常失败。宿主检索接线与模型是否正确采用信息分开报告。最终批次若仍有失败，上表和机器报告直接保留，不宣称彻底修复模型推理能力。

## 自动化与证据

最终全量耗时988.8秒，冻结源码332文件，运行期间源码变更0；完整JUnit和日志位于`full-validation-final-runtime/`。较早中止的两批全量测试保留为中止，不计通过；中间版本完整959/959的回执保留于`full-validation-release/`，不拿它替代最终代码验证。早期回归曾发现纯工具压缩重排问题，已修复并保留单独失败日志。

本地证据根：`artifacts/long-workflows-2026-11-12/`。

- `live-v1/`到`live-v4/`、`memory-followup-v1/`：freeze、源码快照、初始夹具、逐轮输入/回答、请求、服务回执、任务树及工具轨迹、审批决定、JSON/CSV外部验收、追加式用量账本。
- 各批`evidence-sha256.json`：原始登记证据哈希；`audit.json`汇总逐文件校验、按真实执行者去重、子会话绑定及实际请求必保帧校验。
- `baseline-v2.xml/log`及`baseline-sources/`：首批离线发现和初始源文件；初次测试目录未建导致的15项setup error也保留，不算工程功能失败。
- `post-v1-runtime.diff`：初批后的代码差异；最终公开源文件直接位于项目内。真实任务不需要生成补丁，文件v1→v2由冻结宿主步骤注入，模型文件diff为空。
- `build_report.py`：从原验收和审计生成本文与机器汇总，不改原始金标或原判定。

所有已结束批次运行期源码不变，登记证据及源码快照哈希校验见机器记录。本地实际Key精确扫描命中数0；不抓取鉴权头，不保存模型思考正文。公开文档和README只留汇总，原始会话产物默认本地保留。

复现与预算注意事项见[方法说明](long-workflows-method.md)。独立验证器和离线测试不产生模型调用；带`--live`会收费，输出目录必须新建，重新实验前仍须核对累计账本和授权预算。
