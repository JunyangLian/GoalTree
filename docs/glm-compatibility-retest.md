# GLM 兼容性复测：保持主分与验收不变

2026-11-06。[首轮真实评测](../validation/glm-repair30-baseline-2026-11-06/report.md)原计划 30 题 × 2 Agent × 1 次。记录完核心 24 次尝试后，最后一个 worker 因连续响应拒绝和重试触发 645 秒截止，按冻结规则停止，36 个槽位保持未启动。两方核心可验证修复分均为 83.33（各 10/12），不是完整 30 题成绩。

首批还暴露了资源导出的遗漏：终止任务的部分响应有已知 Token，不能因为缺终态汇总而漏算；该任务耗时为调度至终止 645.642 秒，与常规 Agent 至第一次外验的计时口径不同。公开报告已补齐全部已知响应使用量，未知请求仍留保守预留；初版导出以 `.initial` 保存，原始记录和主分不变。终止后独立复验确认该工作区源码未修改、测试未通过，并非静默丢掉一个可能成功的运行。

## 修正一个本地协议假设

原适配器根据 `completion_tokens_details.reasoning_tokens` 判断每轮必须有 `reasoning_content`，仅允许明确为 0 的例外。真实记录反复出现工具调用完整、但没有正文且用量报正数的响应，导致本地拒绝并反复请求。

[官方说明](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)要求在交错和保留式思考中回传已有思考，保留原文与顺序；该页面没有给出“可用 Token 计数重建缺失正文”的协议。本项目据此采用如下兼容策略，属于本地实现选择，不声称已证明服务端计量或返回有错：

- 返回思考字符串时，逐字保留并沿原生 assistant/tool 序列回传。
- 字段缺失或为 null 时，不捏造内容，也不依据 Token 用量要求模型“补发”不存在于响应中的字段；保留缺失标记供审计。
- 非字符串、非 null 的畸形字段仍拒绝；截断、错误模型、非法调用参数、路径和权限检查不变。
- Thinking enabled、clear_thinking=false、reasoning_effort=max、4096 输出 Token、20 工具步、30 请求、180 秒 API 超时、645 秒 worker 截止、单并发均不变。
- 同时修正 CodeTrail、ApeCode 的适配器和共同观察器。ApeCode 上游循环不变；两系统对截断和其他错误的原生恢复机制仍有差异，结果不能当作上游默认 CLI 的排名。

这是协议兼容修正，不是放宽工具授权或验收条件。DeepSeek 等其他 provider 的原有字段校验保持原样。项目协议单元测试的预期随真实合同修正，新增缺失字段、错误类型、原样回传及其他 provider 不变的验证；**Agent 修复任务的冻结验收测试没有修改**。

## 受控方案与资源

另立批次，对原本首次运行前就选定的完整核心 12 题复测：四类各 3 题，两 Agent 各一次，共 24 次。不是根据失败选题，不把重复当新任务，不修改主分公式或金标；原批次和所有失败全部保留。这些题已经参与开发和接口排查，仅称已知回归兼容性验证，不能称留出泛化结果。

复用同一故障源码、测试、题面、顺序、上游源码、Python 和安全策略。新请求首次发送前冻结新 Harness 源码及协议；预算继承首批完整账本，包括失败成本和未确认请求预留。两批合计仍受 **2,000,000 Token / 标价上界 5 元**限制，任一先到停止；不足完成时不丢分母，也不发布虚构完整分数。

```powershell
# 将普通 API 密钥安全置于当前进程环境 BIGMODEL_API_KEY 后运行。
# 在新的检出/输出目录复现，保留历史账本，不能重置预算。
python -X utf8 scripts/run_glm_benchmark.py --prepare --core-only --output artifacts/your-compat-retest --python C:\path\to\python313.exe --test-python C:\path\to\python310.exe --prior-budget artifacts/your-baseline/budget
python -X utf8 scripts/run_glm_benchmark.py --run --output artifacts/your-compat-retest
```

结果只在实际完成后报告。主分变化不能单独解释为广泛代码能力提升；协议拒绝消失、已有思考原样回传、实际工具结果继续送达和冻结测试通过，才是本次根因修正的直接证据。稳定性仍需独立三次重复，本轮每题一次不计算稳定分。
