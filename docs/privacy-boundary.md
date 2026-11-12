# GoalTree 隐私边界 v1

本模块实现数据分级、派生来源继承、受控出口及不含正文的隐私决策审计。它不是任意秘密的语义识别器，也不是企业合规认证。私有模型、Vault服务与供应商ZDR合同不在本版完成范围。

## 数据分级与来源链

宿主配置`.pico/privacy/policy.json`指定目的地origin（协议、域名、端口）、用途、最高等级、观察/执行模式及审计保留期。标签与来源边保存在`.pico/privacy/state.sqlite3`。这些不是Agent工具；Agent不能降级标签、修改策略或以普通工具审批绕过核心禁外发。

| 等级 | 默认策略 |
|---|---|
| public / 公开 | 仍须目的地和用途授权；不能覆盖检测出的敏感内容 |
| general / 一般 | 未分类会话按一般档处理，可向已配置的一般档目的地发送必要上下文 |
| sensitive / 敏感 | 执行模式下，仅可发往宿主明确授予敏感档权限的服务；默认外源模型没有该权限 |
| core / 核心 | 禁止外发；明确配置的数字loopback服务可在本机处理，但必须独立核验其日志和转发设置 |

来源链取最高等级。摘要、裁剪、改写、记忆、Skill、证据、文件修改或新会话都不自动降级。后续提升源文件等级会影响已记录的派生链。v1标签只升不降，尚无经过独立核验的去标识化降级工作流。

任务继承Goal、父任务和前置任务；执行会话绑定当前Goal/Task。消费被分类文件后，会话和当前Goal保守继承其等级，因此同Goal后续工作可能受限。新Goal与不相关Goal不自动继承；但读取同一个派生文件或记忆时仍会继承。文字附件在原始提取文本脱敏前分类，附件ID进入会话来源链。原始附件和用户源文件保留本地，不会自动改写或擦除。

文件列表、检索来源、代码改动和沙箱验证输入建立来源边。关系图使用已访问集合处理循环，超出边界拒绝。宿主外部复制、历史未记录来源、未识别的自然语言秘密及任意隐写不在自动追踪保证内。标签是政策，不是对文件内容真实敏感度的认证。

## 统一检查与执行边界

`pico/privacy.py`为统一决策层，显式接入现有传输，没有全局monkeypatch或TLS中间人解密。覆盖已配置工作区的Agent循环、嵌套任务/委派、已绑定模型客户端，以及模型、远程Embedding、搜索、公开网页、Search MCP和固定Hosted MCP。扩展页检查与公共Improver请求同样经过策略。并发搜索显式继承作用域。

检查实际请求内容、URL和自定义Header；认证Header由可信适配器附加，不写入prompt或隐私审计。初始GLM网页策略只授权当前模型origin和既有搜索origin；额外网页域名、MCP或Embedding需由宿主显式授权用途。网页跳转逐跳检查；受保护模型传输拒绝自动重定向，防止认证信息被带往新目的地。

检测包括已知环境变量凭据、部分凭据/个人信息形状、URL编码、嵌套JSON和有限的小段Base64。大小和深度都有上限；不是完整中文NER、OCR、压缩包解析或抗任意编码。邮箱、手机号等公开信息也可能被保守拦截，需评估可用性损失。

被分类语料不进入远程Embedding，私人记忆在保护模式下不使用远程dense；可降级为本地词法检索，不能换服务上传。没有合格本地服务时保持拒绝，不自动切换外源模型。

无法检查的内联图片/二进制阻断外发，尚未实现图片脱敏。模型响应在完整解析、合并流式片段并检查后，才用于工具解析或最终回答。凭据型响应被拒绝，不随意替换工具JSON字段。保护模式不发布未经检查的逐Token正文，但保留连接/分析等无正文进度，因此首段可见正文可能较晚；低延迟增量脱敏尚未实现。原始wire回调关闭，普通传输重试不扩大数据权限。

策略、检查器、状态或审计故障时拒绝dispatch；运行中更改政策会中止后续请求。状态库存在而策略文件删除时不会静默关闭保护。`privacy_denied`有独立失败阶段和界面提示，不等同于超时或任务成功。

启用保护后，宿主Shell和本地stdio MCP被拒绝，因为本版没有它们的可证明egress边界。Windows AppContainer仍只支持原有固定批准的unittest命令，网络能力关闭；不放宽ACL、审批或命令白名单。网络由可信宿主适配器执行，新插件/传输必须接入策略。没有实现全机器代理、任意进程网络接管或通用私有模型路由。

信任边界包括宿主、Harness安装、政策管理员和固定适配器；不防御有权修改这些组件的管理员或被入侵宿主。应将可信Harness安装与Agent可改目录分开。数字loopback也不能证明服务不会再转发。普通私网域名不自动获得私有环境信任。被授权的数字loopback请求强制直连，不继承HTTP代理，避免把本地核心请求送往代理。

## 凭据、审计与本地记录

真实凭据仍来自本地环境/配置，由宿主客户端放入认证Header；不提供给模型或AppContainer，不宣称已部署Vault、凭据轮换或OS凭据保险库。

隐私审计只含时间、用途、目的地HMAC标识、等级、决定、规则编号、字节数、检查时长和模式；不含prompt、查询、路径、命中文本、认证值或正文哈希。审计写失败阻断dispatch。

本地任务树、会话、工具证据与恢复记录是另一类私有数据，仍可能含正文。已知凭据/canary在JSON记录中掩码；Web在Agent运行前保存的入口快照也处理掩码，原始等级通过来源边保留，防止重启后因文字变成占位符而错误降级。原始附件及源文件例外：它们作为用户输入本地保留。

审计默认30天，可设1–90天，在下一次审计写入时逻辑清理过期行；不覆盖所有本地转录、附件、备份、SQLite空闲页或供应商日志，不代表物理安全擦除/ZDR。本地数据使用已有OS访问权限，未新增静态加密与完整生命周期删除服务。不能把metadata-only审计宣传为“整个产品不存prompt”。

## 配置与复现

默认GLM网页启动器为新选文件夹初始化执行策略，不覆盖已有策略。通用CLI及历史入口在工作区存在策略时自动应用；未初始化的旧工作区不声称已经受保护。

```powershell
python -m goaltree.privacy --workspace D:\my-workspace init --model-url https://open.bigmodel.cn/api/paas/v4 --web
python -m goaltree.privacy --workspace D:\my-workspace label file:private-notes.txt core
python -m goaltree.privacy --workspace D:\my-workspace label goal:goal_实际ID sensitive
python -m goaltree.privacy --workspace D:\my-workspace allow https://learn.microsoft.com --purpose web
python -m goaltree.privacy --workspace D:\my-workspace status
python -m goaltree.privacy --workspace D:\my-workspace audit
python -X utf8 scripts/eval_privacy.py --output artifacts/my-new-privacy-evaluation
```

本地模型可在初始化时明确指定`http://127.0.0.1:11434`，本版不自动启动或认证该服务。`init --mode observe`只用于合成/获准数据：会允许已授权目的地的敏感档请求并记录would_deny；核心、未知目的地、无法检查的图片仍拒绝。不要用真实核心资料验证影子模式。

## 合规与主流方案参考

不训练、ZDR、驻留、保留期及第三方工具条款需要分别核验，本地设置不能替供应商作保证。个人信息出境的告知、单独同意、影响评估、适用条件/豁免需结合具体场景判断；v1不自动给出法律结论。

参考：[OpenAI沙箱与凭据边界](https://developers.openai.com/api/docs/guides/agents-api/environments/security)、[Claude Code沙箱设计](https://www.anthropic.com/research/claude-code-sandboxing)、[Anthropic数据保留](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention)、[个人信息保护法](https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm)、[数据跨境流动规定](https://www.cac.gov.cn/2024-03/22/c_1712776611775634.htm)。它们支持设计原则，不代表GoalTree拥有相同保证或合同。
