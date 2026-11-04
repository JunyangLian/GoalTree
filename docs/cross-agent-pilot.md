# 同模型代码 Agent 小规模横评

## 对照选择

首轮使用 CodeTrail 与 [ApeCode](https://github.com/ApeCodeAI/apecode)，不使用 Codex CLI。

ApeCode 同为 Python 本地代码 Agent，核心是原生函数调用、独立工具注册、线性消息历史和有界主循环。其公开 `ChatModel` 协议允许接入同一模型，而不重写 Agent 循环。固定上游提交为 `8d85a3929cac72665e96f10d1dcbcce2962ca49b`，Apache-2.0；源码与许可证随技术证据保存，保留归属。

这是一个体量较小的实现，不能代表全部开源 Agent。选择依据是结构可比、协议兼容、可冻结和可审计，没有依据试跑成绩筛选对照。

[mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) 为后续候选，其 Bash-centric 执行环境与当前 Windows 固定 unittest 后端不同。当前未安装可用容器运行时；不通过开放宿主 Shell 强行接入，不把环境不支持算成该 Agent 的修复失败。[官方环境说明](https://mini-swe-agent.com/latest/faq/)。

nanocode 的 README 提到兼容代理，但固定提交 `995e0413444886a1154be45e8f641031a57a88d7` 的 `api_client.py` 实际以 Anthropic 消息格式调用；不能直接等同于硅基流动 Chat Completions。首轮不增加协议转换层。[项目与源码](https://github.com/nanocode-project/nanocode)。

## 已固定的规模与口径

4 道不同任务 × 2 个 Agent × 每题 2 次 = **16 个计划运行槽位**。

| 任务 | 类型 | 来源 |
|---|---|---|
| pagination_boundaries | 单文件逻辑 | 已有自编任务 |
| user_lookup_contract | 跨文件契约 | 已有自编任务 |
| boltons_isoparse | 未知位置定位 | 已有开源故障最小复现 |
| humanize_fractional | 回归兼容性 | 已有开源故障最小复现 |

全部来自已有冻结资产、此前已在本项目评测，不是新留出集。开源题是裁剪复现，不是完整仓库任务。每题的原始描述、允许修改文件、可见验收测试及 SHA256 不变；不将重复运行称为独立任务。

两边使用硅基流动 `deepseek-ai/DeepSeek-V4.1-Flash`，原生 Chat Completions，temperature=0.2、top_p=0.95、Thinking开启、thinking_budget=4096、max_tokens=4096；20次独立工具调用、300秒运行墙钟上限、并发1。请求参数不代表服务商披露了权重版本或采样器实际值，返回模型ID须核对，不切换模型。

每次在仓库外建立全新工作区，模型与会话重新创建；不加载全局用户配置、旧会话或外部参考补丁。控制器、答案来源信息、结果目录与外部验收副本均在 Agent 工作区之外。

## ApeCode 适配范围

- 原上游 `NanoCodeAgent.run`、消息历史、原生工具调用处理和默认工具实现保留，固定源码 SHA。
- 用公开 `ChatModel` 接口注入独立记录的硅基流动 HTTP 客户端；保留原始 `reasoning_content` 与 `tool_calls`，不引入 CodeTrail 的协议修复逻辑。
- 用宿主策略检查每个工具参数，文件修改只允许冻结实现文件；`exec_command` 仅接受精确 unittest 命令，通过现有 AppContainer 后端执行，绝不调用上游默认无限制宿主 Shell。
- 原系统提示模板保持；目录规则发现限定在任务根，时钟固定为协议冻结时间。没有导入用户插件、Skill 或子代理。
- 原循环的 max_steps 是模型轮次，因此额外在每个工具执行前实施20次调用硬上限。不能只设置20模型轮次就宣称20工具步。
- ApeCode 使用其要求的 Python3.13，CodeTrail 使用现有 Python3.10；测试执行和独立验收均使用对应进程的 AppContainer 私有运行时，版本差异记录在环境配置中。

这是**受控适配的 Harness 对比**，不是 ApeCode 默认 CLI 的性能排行榜。两边工具集合、系统提示、流式传输、上下文处理仍不同，结果不能归因于某一个记忆或检索功能。单会话小题也不验证长期记忆的跨会话收益。

## 独立判定与保留

功能验收率 = 外部验收通过且最终文件范围合法的运行数 / 已启动运行数。

流程成功率还要求：Agent 正常结束、修改前观测到原始失败测试、最后修改后的同一测试真实通过且文件快照仍一致。最终答案不是验收依据。独立验证器从冻结文件重新建立副本，只覆盖允许修改文件的最终字节，在 AppContainer 内重测；执行期间改变工作区的通过结果无效，失败关闭，不回退宿主。

保留协议、计划、冻结清单、来源/上游源码快照、每次模型请求与响应（无Authorization）、工具输入输出、审批拒绝、源码差异、最终文件和独立验收。JSON/CSV分开记录计划数、已启动数、未启动数、功能/流程成功、工具调用、Token、耗时与失败分类；未知Token不是0，无法归因的真实费用不推算。

不替换已启动槽位；中断目录即使没有最终结果，也记为已启动异常记录。传输有限重试属于原运行。非任务API可用性预检若返回402等错误，正式运行不启动，不能把16个未启动槽位说成0/16修复失败，也不能报告成功率。

## 可运行命令

```powershell
git clone https://github.com/ApeCodeAI/apecode.git artifacts/cross-agent-sources/apecode
git -C artifacts/cross-agent-sources/apecode checkout 8d85a3929cac72665e96f10d1dcbcce2962ca49b
uv python install 3.13
$pilotPython = uv python find 3.13

# 原始上游循环+真实文件/工具/AppContainer，决策明确为脚本；不是模型评测
& $pilotPython -X utf8 scripts/check_cross_agent_adapter.py --output artifacts/your-new-adapter-check

# 先冻结，然后才启动；目录必须新建
python -X utf8 scripts/run_cross_agent_pilot.py --output artifacts/your-new-cross-agent-pilot --freeze --external-python $pilotPython
# 密钥只来自本地 SILICONFLOW_API_KEY 环境变量，不能写在命令行或仓库文件中
python -X utf8 scripts/run_cross_agent_pilot.py --output artifacts/your-new-cross-agent-pilot --run
python -X utf8 scripts/run_cross_agent_pilot.py --output artifacts/your-new-cross-agent-pilot
```

冻结后代码/任务/配置/外部Agent版本发生变化会拒绝继续。要测试新实现，使用新输出目录并保留旧结果。原任务首次冻结资产从未修改。
