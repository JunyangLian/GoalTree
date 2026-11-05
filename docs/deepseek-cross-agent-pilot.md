# DeepSeek官方同模型横评：方法与复现

2026-11-05。先阅读[报告](../validation/cross-agent-deepseek-pilot-2026-11-05/report.md)和[受控适配范围](cross-agent-pilot.md)。本轮不修改CodeTrail核心权限和冻结测试。

## 冻结与环境

- 模型 `deepseek-flash`，官方OpenAI兼容接口 `https://api.deepseek.com/v1`；文档指向V4.1-Flash-1017，服务器别名可能可变，以实际响应模型及fingerprint留档，不静默切换。
- 两边Thinking开启、`reasoning_effort=low`、`top_p=0.95`、max_tokens=4096。Thinking下temperature不生效，所以不发送。官方接口不使用硅基流动的enable_thinking/thinking_budget参数。
- 原生tool_calls和reasoning_content完整回传；CodeTrail使用既有原生机制，不将DSML文本当工具执行。ApeCode保留其原始循环，通过公开ChatModel协议接入。
- 每运行全新工作区和会话，工具调用20次、模型请求30次、墙钟300秒、并发1、每请求最多1次传输尝试；两边同样限制，不补跑已启动槽位。
- Agent运行：CodeTrail Python3.10.1、ApeCode按上游要求Python3.13.11。测试执行使用AppContainer；独立第二次验收统一在报告控制器Python3.10.1的AppContainer中执行，差异明确披露。
- 相同4题、同一可见测试和用户提示，全部此前评测过；2自编、2开源故障裁剪，非留出集、非完整仓库。
- ApeCode提交 `8d85a3929cac72665e96f10d1dcbcce2962ca49b`；上游源码不打补丁，Apache-2.0许可证与源码快照保留。替换执行后端、限制规则发现、加入预算均是明确的外部适配。

父 `freeze.json` 冻结通用控制器、任务、提示、上游与基础源码；`official-freeze.json` 进一步冻结官方接口驱动、预算支持、相关回归和父清单SHA。两个清单共同约束正式执行，不能只校验其中一个。

## 费用限制

初始账户2.71元，整个实验窗口上限2.50元，至少保留0.21元。每次付费请求前查询余额，并以UTF-8序列化长度加消息框架余量估算输入上界、按高峰未缓存输入与最大输出预留。这个估计不是服务商预分词账单；返回usage另按公开费率记录，未观测的请求继续保留预留额，不能记作0。

不同批次可能复用run_key；导入历史成本账本时为历史请求ID加命名空间，原始账本、映射前后SHA和所有金额保留。正式运行开始后禁止重做该初始化。

接口参数与费率依据：[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[Thinking模式与原生回传](https://api-docs.deepseek.com/guides/thinking_mode/)、[余额接口](https://api-docs.deepseek.com/api/get-user-balance/)。官方模型价格有高峰/空闲差别，不能把空闲价套用全部请求。

## 从新环境复现

需要Windows、Git、uv、项目现有Python，以及可用的AppContainer环境。API Key在本地环境变量DEEPSEEK_API_KEY中提供，不能放入命令参数、报告或仓库。

```powershell
git clone https://github.com/ApeCodeAI/apecode.git artifacts/cross-agent-sources/apecode
git -C artifacts/cross-agent-sources/apecode checkout 8d85a3929cac72665e96f10d1dcbcce2962ca49b
uv python install 3.13.11
$pilotPython = uv python find 3.13.11

# 准备通用资产和外部环境元数据；不调用硅基流动API
python -X utf8 scripts/run_cross_agent_pilot.py --output artifacts/cross-agent-pilot-2026-11-05 --freeze --external-python $pilotPython

# 非正式任务的协议往返及实际入口小样例；也受同一费用窗口限制
python -X utf8 scripts/run_deepseek_pilot.py --output artifacts/your-new-official-batch --probe
python -X utf8 scripts/run_deepseek_pilot.py --output artifacts/your-new-official-batch --entry-smoke

# 首次正式调用前冻结；每个批次用全新目录
python -X utf8 scripts/run_deepseek_pilot.py --output artifacts/your-new-official-batch --freeze
python -X utf8 scripts/prepare_pilot_budget_import.py --output artifacts/your-new-official-batch
python -X utf8 scripts/run_deepseek_pilot.py --output artifacts/your-new-official-batch --run
python -X utf8 scripts/run_deepseek_pilot.py --output artifacts/your-new-official-batch
```

如果已保有通用冻结目录，不重复freeze或覆盖旧资产。实际归档中的修正批次为 `artifacts/cross-agent-deepseek-pilot-v2-2026-11-05`。源码/资产变化时原批次拒绝继续，另建版本并保留历史。

独立复验与报告：

```powershell
python -X utf8 scripts/report_deepseek_pilot.py --output artifacts/cross-agent-deepseek-pilot-v2-2026-11-05 --public validation/your-new-audit-report --audit
python -X utf8 -m pytest tests/test_deepseek_pilot.py tests/test_cross_agent_pilot.py tests/test_deepseek_protocol.py tests/test_final_eval_controller.py tests/test_repair_suite.py tests/test_repair_suite_audit.py -q
```

报告脚本同时复验首批历史目录；新环境若要复现本次历史审计，应先从证据包恢复两个批次到相同相对位置。要评测新批次，应按本次控制器方式为其单独报告，不假装拥有不存在的历史运行。归档中的绝对路径用于审计，不承诺在另一台机器原路径续跑；新建工作区重做实验。

## 接入错误与纠正

首批原控制器按 `apecode_pilot_adapter` 导入，官方驱动按 `scripts.apecode_pilot_adapter` 适配。Python因此加载两份模块，模型工厂未注入到实际worker入口。6条ApeCode请求前KeyError、3条CodeTrail成功与7槽位未启动均保留。

纠正只涉及适配器模块别名绑定和预检覆盖：同名入口绑定同一模块，新增真正worker导入路径回归，并用真实模型完成独立加法小样例后另冻完整16次批次。没有重写ApeCode算法、向正式任务添加答案或修改验收标准。

这次记录说明：只检查HTTP接口或直接调用某个类，不能证明控制器实际执行入口已正确接入。小规模横评的价值包含发现评测系统自身的接入错误。
