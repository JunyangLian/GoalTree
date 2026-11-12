# 隐私边界工程验收 · 2026-11-11

本轮是机制与集成验收，没有请求GLM生成回答，没有私有模型效果对比，也不并入历史代码修复或目标规划Benchmark。真实模型判断隐私的准确率、未见秘密识别和生产误报率均未评估。

## 实施与边界

已实现宿主四档标签、持续来源图、分用途目的地检查、无正文隐私审计。默认GLM网页启动器为新选工作区初始化执行策略；旧通用入口没有策略时保持原配置，不能冒充全产品已经强制启用。

覆盖原生/兼容模型请求、本地Ollama入口、远程Embedding、GLM搜索、免费国际搜索、网页读取、Search MCP、固定Hosted MCP、扩展连接检查和公共Improver。只读委派与并发搜索显式继承作用域；绑定模型客户端脱离普通循环后也保留检查。任意新增宿主网络代码需要重新接入，不是全机器网络代理。

核心来源的摘要、记忆、Task上下文、证据和文件改动保持等级，重启和压缩不降级；根目录标签覆盖子文件。Web入口快照掩码前先保留原等级，文字附件在提取文本掩码前分类。原始输入附件/文件仍留在本地，不宣称所有本地存储无敏感信息。

策略/状态/审计异常拒绝发送。宿主Shell和stdio MCP在保护模式下阻断，固定AppContainer验证仍按原审批执行且不扩大网络能力。未接Vault、没有私有模型自动回退、没有供应商ZDR或PIPL认证。无法检查的图片暂不外发；受保护响应先完整检查再显示，尚无逐Token安全输出。

## 冻结合成外发测试

24个不同的合成案例首次执行前冻结于`benchmarks/privacy_v1/cases.json`。每次独立工作区、策略与会话；执行前保存金标/运行器/策略实现/接收器及TLS测试证书哈希。参考预期不传入运行上下文。没有根据结果修改这24例预期。

本机独立TLS接收器记录实际HTTP到达的路径、Header和body，保持证书及主机名校验；证书和私钥明确为公开测试夹具，不装入系统信任库。这里以localhost主机名模拟**外部信任等级**，不把它当作数字loopback可信本地模型。测试不使用生产凭据、不访问公网。

最终批次位于本机忽略目录`artifacts/privacy-2026-11-11/redteam-release/`：

| 指标 | 结果及分母 |
|---|---|
| 案例预期一致 | 24/24 |
| 禁止外发却到达接收端 | 0/16 |
| 合法案例未到达接收端 | 0/8 |
| 实际接收HTTP请求 | 8 |
| 真实外源模型调用 | 0 |
| 公网请求 | 0 |
| 单例完整路径时长p50 / p95 | 44.89 / 2232.325 ms |

时长包含放行、阻断、Python客户端、本机TLS连接与响应，不是纯DLP额外耗时或生产时延；没有未保护时延对照。样本小，不能据此宣称生产泄漏率/误报率为0。8个合法样本是清晰合成输入，未代表含公开邮箱等保守规则冲突的真实分布。

案例覆盖明确标签、凭据canary、密码赋值、邮箱、URL编码/二次编码、JSON转义、Header/URL渠道、未知目的地、图片拒绝、MCP/Embedding内容及派生摘要。目录根标签、沙箱快照、记忆与Web持久化等进一步由专项软件回归验证，不混入24例分母。

保留`redteam-v1/`、`redteam-v2/`、`redteam-final/`、`redteam-final-v2/`及最终批次。共5批，都是修复过程中相同24例的工程重放，不是120个独立问题或120次模型能力测试；各批均保留接收端原始记录、逐例结果、审计、CSV及哈希。重放原因分别包含隐私状态保护、Web入口落盘和根目录继承补齐，未选择性替换失败或扩大金标。

## Web集成与实际浏览器限制

后续补验：已经通过 Tabbit 对真实页面执行输入、点击和刷新，并修复拒绝提示、脱敏回显及历史恢复问题；普通消息已完成一次真实 GLM 调用。详见[浏览器补验报告](privacy-browser-validation-2026-11-11.md)。下文保留初次交付时的限制记录，不能用初次全量回归覆盖后续源码变更。

最终预览`http://127.0.0.1:8785/`使用新独立工作区、GLM-5.3-Flash普通API配置、AppContainer与执行模式隐私策略。通过真实HTTP向`/api/tasks`提交合成canary，任务以`privacy_denied`失败，工具步数0，审计为核心禁外发，没有放行的模型出口记录。回复和JSON恢复记录均未含原始canary；这次检查未调用外部模型。验收后切换到新的空白对话，先前的失败轨迹及核心会话标签保留，没有清空历史。详见本机`release-api-result.json`与`release-api-checks.json`。

浏览器自动化返回“Local browser use is disabled in Work settings”，因此本轮**未完成真实浏览器点击/截图验收**；没有切换浏览器或将接口检查冒充浏览器E2E。界面状态标签与错误分类由前端/后端自动化覆盖，不能替代视觉核对。

8783旧预览的重启被自动审批以“blocked by policy”拒绝，旧进程保留；8785为独立新入口，没有覆盖用户旧工作区或绕过拒绝去停止旧服务。8782/8783/8784属于本轮早期验证进程，不是最终交付入口。

## 缺陷修复与保留记录

- 核心请求已拦截但异常继续抛出：现在有独立privacy_denied停止原因与界面说明，保留当前状态。
- Web在Agent运行前保存快照：增加强制凭据掩码与持久来源，避免入口与恢复绕过保护。
- 附件提取时掩码导致等级信息丢失：在原始提取文本上分类，附件ID继承到会话。
- 固定验证命令可读取整个快照：继承被分类输入，未修改Shell命令/权限边界。
- 旧记忆检索的远程dense路径：保护模式不上传该内容，保留本地词法能力。
- 根目录标签未覆盖子文件：明确覆盖整个子树并添加独立回归。
- 本地模型可能继承HTTP代理：明确授权的数字loopback强制直连，避免核心请求经过外部代理。

开发期专项失败、参数错误与中止回归均保留于本机`artifacts/privacy-2026-11-11/`。根目录`full-tests.*`、`final/full-tests.*`和`verified/full-tests.*`是为补修而中止的回归，不能计作全量通过；其摘要记录源文件变化。`final-validation/`也因补齐loopback直连而中止。最终全量回归与哈希见`release-validation/`，最终结果见文末。

## 复现

```powershell
python -X utf8 -m pytest -q
python -X utf8 -m pytest -q tests/test_privacy.py
python -X utf8 scripts/eval_privacy.py --output artifacts/my-new-privacy-run
python -m goaltree.privacy --workspace artifacts/my-workspace init --model-url https://open.bigmodel.cn/api/paas/v4 --web
python -m goaltree.privacy --workspace artifacts/my-workspace label file:private-notes.txt core
python -X utf8 scripts/run_goaltree_glm.py --workspace artifacts/my-workspace --port 8785
```

运行器拒绝复用已有输出目录。策略与部署限制详见[隐私机制](privacy-boundary.md)。本轮未推送GitHub，也未改动历史Benchmark成绩。

## 最终自动化结果

全量自动化 **871/871通过**，失败0、错误0、跳过0；耗时760.03秒。冻结339份源码与资产，运行期间及交付核对时未变化。38项隐私专项包含在全量测试中，不相加。`git diff --check`通过，当前本地配置中2份实际凭据值的待提交文件扫描未发现泄漏。机器汇总见[检查数据](privacy-checks-2026-11-11.json)。

证据目录：`release-validation/full-tests.log`、`full-tests.xml`、`full-source-freeze.json`、`full-test-summary.json`；`redteam-release/`中的逐例JSON、`runs.csv`、`receiver.json`、`freeze.json`；`release-independent-audit.json`；`release-api-checks.json`和`release-api-result.json`。这些含本机完整合成轨迹的文件留在忽略目录，公开文档只记录方法、结果与复现命令。
