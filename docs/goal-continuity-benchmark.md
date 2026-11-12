# Goal continuity v1

六个自设计开发用例，验证压缩上下文中的状态、偏好与证据消费，不是代码修复基准、完整Agent任务、留出集或主流产品排行榜。每例三种上下文呈现，各一次真实GLM请求，总18个单请求会话；不是18道独立任务，也未测稳定性。

`tasks.json`包含外部判定字段，仅由运行器评分，不进入模型请求或Agent工作区。`protocol.json`冻结模型、服务商、采样、输出预算、字节预算、超时、并发、分母和失败处理；运行前再冻结资产、运行器、Harness及旧压缩器的SHA256。

三种呈现为旧压缩器的普通会话、新任务树上下文、新任务树加已授权范围内有效记忆；所有组均在相同输入预算下经历长历史压缩。源资料为公开的合成测试夹具，含人为添加的无关文本，不是采集的用户对话。由于呈现和压缩器同时不同，不能将差异解释为某个单模块的因果效应。

本轮唯一正式真实调用批次已保留在本地`artifacts/long-goals-2026-11-11/live-v1/`。后续根据失败修复代码，仅做离线重放和工程回归，没有重新调用同组题来替换成绩。参见[报告](long-goal-evaluation-2026-11-11.md)。

```powershell
# baseline指向评测前冻结的旧版native_context.py；output必须为新目录
python -X utf8 scripts/eval_goal_continuity.py --output artifacts/my-context-probe --baseline <frozen-native_context.py>
# 如需独立新批次，明确使用--live；旧结果不得覆盖，不把开发复测当留出集
python -X utf8 scripts/eval_goal_continuity.py --live --output artifacts/my-context-probe-live --baseline <frozen-native_context.py>
```

正式实验的冻结副本包含本文件创建前的源清单；不要用后来版本的运行器或Harness冒充原批次。API Key只读取本地普通`BIGMODEL_API_KEY`或`.env.glm.local`。计费实扣未知，报告已知Token与缺失用量，不补零。读取公开输入与产生JSON判断，无Shell、联网搜索、修改文件或外部业务操作，工具步数为0。
