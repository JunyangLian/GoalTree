# GLM 普通 API 真实试验与复现

2026-11-06 已完成普通 API `glm-5.3-flash` 真实预检、原生工具结果回传和双 Agent 小规模试验。这个入口与 `run_process_controller.py` 的离线夹具分开；完整证据和限制见[报告](../validation/glm-live-2026-11-06/report.md)。

## 先复核已有结果（不调用模型）

读取 `validation/glm-live-2026-11-06/runs.csv` 与 `analysis.json`。原始证据包及第二次审计包在 `artifacts/`，其 SHA256 分别登记在报告目录的两个索引。原始包保留两个批次，不只保留通过的复测。

对结果包重新验收可使用 `scripts/report_glm_live_pilot.py` 中的 `audit_batch(batch, output)`：它读取冻结任务和最终合法文件，在**新的 AppContainer 副本**运行原测试，核对源记录未变；不依赖 Agent 的成功声明，不读取参考补丁。指定新的审计目录，保留已有输出。

```powershell
python -m pytest -q --junitxml=artifacts/your-glm-regression.xml
```

完整测试在本轮最终 Harness 上为 454 项通过，相关回归 79 项通过。需要 Windows AppContainer 环境；本机未验证 Docker 或 SWE-bench 的 Linux 执行内核。

## 新建真实试验（会产生 API 用量）

入口：`scripts/run_glm_live_pilot.py`；标准 API 密钥仅从进程变量 `BIGMODEL_API_KEY` 读取。不要把密钥写入参数、脚本、轨迹或提交。不要使用 Coding Plan 凭据。

以下命令用于**独立的新检出目录**，其中不存在本轮同名历史输出。先安全地将密钥置于该终端的进程环境，安装项目依赖和此前锁定的 ApeCode 源码，并准备 Python 3.13 worker。ApeCode 默认位于 `artifacts/cross-agent-sources/apecode`；上游文件版本必须与本轮 `external.json` 所列 SHA256 对齐。它不由此入口自动下载。

```powershell
# 先测普通 API 是否可用；只有可用且模型一致才继续
python -X utf8 scripts/run_glm_live_pilot.py --output artifacts/glm-live-pilot-2026-11-06 --availability

# 真实两轮工具协议检查；成功之后才允许冻结任务
python -X utf8 scripts/run_glm_live_pilot.py --output artifacts/glm-native-protocol-2026-11-06 --probe

# 指定本机实际 Python 3.13 绝对路径
$workerPython = 'C:\path\to\python-3.13\python.exe'
python -X utf8 scripts/run_glm_live_pilot.py --output artifacts/your-glm-dev-pilot --freeze --python $workerPython
python -X utf8 scripts/run_glm_live_pilot.py --output artifacts/your-glm-dev-pilot --run
```

`--availability` 是单独有界的连接检查（最多 256 输出 Token），不会自动启动修复题。协议检查建立 1 元共享预算账本并导入已保存的连接检查用量；原环境代理失败请求若不存在则不虚构。既有日期固定窗口为 `artifacts/glm-budget-window-2026-11-06`，后续同目录的试验共享其剩余额度，不能删除账本重置额度。

冻结前要求 `artifacts/processbench-v0.1-assets-2026-11-06` 的原始任务资产和 asset-lock 完整；如仅克隆了 Git 仓库，须先按[离线资产说明](benchmark-program-offline.md)生成并核对。锁定两道 `dev` 题，不能把此脚本的新目录当作新增独立题目或留出评测。

原始批次不可直接在当前新源码上续跑：冻结检查会拒绝不匹配。历史重演应在独立副本恢复相应 `source.zip` 中源码和锁定的外部文件；本轮脚本不覆盖已有槽位或替换失败。新的真实复跑会有模型随机性，不保证复现相同轨迹或成功率。

## 协议和安全边界

- GLM Thinking 仍启用，有文本时原样回传。缺少字段只在服务端明确给出整数 `reasoning_tokens=0` 时兼容，不伪造内容；正数或未知用量的缺失仍拒绝。
- 拒绝响应是模型请求的一部分，不是免费重试。CodeTrail 的原循环可以反馈后继续，ApeCode 的同类正数缺失会终止，本轮未改造其原始循环。
- 两 Agent 使用统一 Python、原生工具结果、审批与路径策略、20 工具/30 请求预算，测试通过 AppContainer 执行，失败不退回宿主 Shell。
- 模型网络请求由控制进程明确直连；隔离内测试无网络授权。这不是给 Agent 任意访问宿主网络。
- 费用是保存用量乘查询日标价的上界估算，账户实扣未知；未知用量保留预留。控制器只适用于冻结的串行预算，不应直接改成并行写同一账本。

`report_glm_live_pilot.py` 默认打包本轮固定两个批次；新试验可复用其审计函数，不能把新结果写进旧报告。公开发布前仍需独立审查过程标签、完成公开基准容器资格并扩大有效样本，本轮没有完成这些条件。
