# 隐私保护：Tabbit 真实浏览器集成验证

日期：2026-11-11。本报告补充此前仅完成 HTTP 验证的部分，不覆盖历史报告或 Benchmark 结果。

## 验证方式与环境

通过已安装 Tabbit CLI 的 Playwright 连接真实 Tabbit 页面，实际执行输入、发送、新建对话和刷新。没有用后端请求代替网页交互，也没有切换其他浏览器。

- 页面：`http://127.0.0.1:8786/`，使用独立工作区 `artifacts/privacy-browser-2026-11-11/workspace`。
- 模型：智谱普通 API `glm-5.3-flash`；密钥从本地配置加载，不写入测试脚本。
- 隐私策略：`enforce`；执行后端配置为 AppContainer，未改变权限。本次对话没有执行 Shell，不能据此声称重新验证了 OS 隔离。
- 测试只使用合成凭据标记和普通短消息，不上传用户文档。

## 结果与实际证据

| 检查 | 结果 | 证据 |
|---|---|---|
| 在输入框填写核心 canary 并点击发送 | 两次请求均以 `privacy_denied` 结束；出口审计两次 deny，无 core allow，工具步数均为 0 | 工作区中的 `runs/*/report.json`、`privacy/state.sqlite3` |
| 中文拒绝提示与消息脱敏 | 对话显示“隐私策略已阻止外发”，用户气泡显示 `<redacted>` | `fixed-visible.json` |
| 刷新后恢复拒绝记录 | 两条主机拒绝消息和活动摘要均保留 | `recovery-visible.json` |
| 点击“＋ 新对话”，填写普通消息并发送 | 真实模型返回“浏览器链路正常”，没有继承旧核心会话的限制 | `benign-submit.json`、`benign-result.json` |
| 再次刷新普通对话 | 回答与“本轮回复已结束”摘要均保留 | `benign-reload.json` |

普通消息仅发生一次模型请求，服务商报告输入 5790、输出 6、合计 **5796 Token**，工具步数 0，页面显示总耗时约 7 秒。未取得账单费用，不估算为免费。核心阻断不消耗模型请求；这是本地策略审计与传输前阻断证据，不是抓包或服务商侧日志证明。

两次核心请求是同一集成场景的重复检查，不是两道独立任务。这三次运行均不计入代码修复 Benchmark 成功率。

## 发现并修复的问题

1. 隐私拒绝此前只显示英文异常和通用失败标题。现在根据停止原因显示中文拒绝原因，活动记录不把“准备请求”描述为“已经发送”。
2. 本地乐观回显会保留原始测试凭据。保护模式先显示隐私检查占位，随后使用服务端脱敏后的消息回显。
3. 拒绝结果未写入对话历史，刷新后只剩用户消息。现在持久化主机拒绝说明，并在恢复的活动摘要中保留 `privacy_denied`。

源码涉及 `pico/web_assets/app.js`、`pico/agent_loop.py` 和 `pico/web_status.py`；行为回归位于 `tests/web_activity.test.cjs` 和 `tests/test_privacy.py`。

## 自动化回归

本次相关回归 **83/83 通过**，失败、错误、跳过均为 0，pytest 输出耗时 28.11 秒：

```powershell
python -X utf8 -m pytest -q tests/test_privacy.py tests/test_web_frontend.py tests/test_web_status.py tests/test_web.py --tb=short --junitxml=artifacts/my-browser-regression.xml
```

前端活动行为脚本单独执行为 18/18，通过 Python 前端测试包装器运行的结果已包含在这 83 项中，不相加。此前 871 项全量通过针对本次浏览器补修之前的版本；本次没有重新宣称全量回归已通过。`git diff --check` 通过。

## 验证限制及失败记录

- 原生截图两次超时；后续同一 Tabbit 页面的 CDP 截图也超出 60 秒硬期限，任务执行器终止，但页面保留。按照恢复说明重新取得该页，先只读核对，然后继续普通对话测试。**没有获得有效截图，未制作替代图片，也不声称完成像素层视觉验收。**
- `bringToFront` 被 Tabbit 的 `foregroundPolicy=preserve` 拒绝；CLI 不支持所提示的 foreground 参数。未更改浏览器设置或绕过限制。
- 一次断言错误地等待顶部状态栏永久显示拒绝原因；页面空闲时顶部状态栏会清空。实际对话已有中文拒绝说明。保留该失败记录，随后检查对话 DOM 和刷新后的历史，没有重复提交来替换失败。
- 环境恢复后首次已执行请求的 CLI 收据不可取回，其持久运行和拒绝审计仍保留；后续重复检查单独保存，不冒充首条收据。
- 本次不覆盖附件、图片、搜索、MCP 以及 Shell 的完整浏览器交互矩阵；相应机制测试见前一轮报告。

## 证据与复现

机器摘要：[浏览器检查数据](privacy-browser-checks-2026-11-11.json)。完整本机证据位于 `artifacts/privacy-browser-2026-11-11/`，包括 CLI 程序及原始收据、失败截图收据、回归 XML/日志、工作区运行轨迹、审计摘要和 `evidence-sha256.json`。原始轨迹保留在忽略目录。

```powershell
python -X utf8 scripts/run_goaltree_glm.py --workspace artifacts/my-browser-workspace --port 8786 --network-route direct --search-backend glm-free
```

首次复现时选择未占用端口，并使用新的工作区。用 Tabbit 打开页面，发送合成核心 canary，刷新核对；再点新对话发送普通短消息。当前验证入口保留在 8786，旧服务和旧工作区没有被停止或覆盖。
