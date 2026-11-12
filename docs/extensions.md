# 插件、技能与 MCP

左侧“插件”打开扩展面板，分为三个页签，支持搜索、预览与按工作区启停。打开面板只读取本地目录，不自动联网、安装软件或调用模型。刷新按钮重新读取本地状态；连接检测由用户单独点击，结果注明检测时间。

## 默认小集合与可选目录

当前目录共12个插件、13项技能、9个搜索/MCP条目。默认仍只启用下面三个插件包及其七项技能；其余按工作区选择。搜索API与MCP在同一连接页展示，但明确标注协议，不将普通HTTP搜索冒充MCP。

| 插件包 | 按需读取的技能 | 来源 |
|---|---|---|
| 目标与报告 | 目标规划、报告提纲 | GoalTree 编写的文本工作流 |
| 资料研究 | 资料研究、文档整理 | GoalTree 编写的文本工作流，复用网页、PDF与附件工具 |
| Superpowers · 开发排障 | 实现计划、系统排障、完成前验证 | [Superpowers](https://github.com/obra/superpowers) MIT 文本技能的精简中文适配 |

默认关闭的可选包：需求澄清（1项）、测试驱动（1项）、代码审查（2项）和会议与写作（2项）。前四项技能适配相同版本Superpowers，会议议程与纪要行动项由GoalTree编写。另外五个插件条目是Context7、GitHub、Playwright、Microsoft Learn和DeepWiki连接的快捷入口，与连接页开关同步；Playwright只提供配置说明，不能启用。

插件包是上述技能的分组开关，不是从远程动态安装的可执行插件。七项上游技能固定于 `22f0b463e7a5e579904dd6211a520321035526f7`，保留作者、MIT许可、原文与适配后SHA256。适配移除了上游客户端专用命令、强制委派、子技能依赖和自动提交要求，遵守用户当前协作方式，不声称完全实现上游框架。

内置技能位于 `pico/bundled_skills/`，目录元数据进入上下文，正文通过 `load_skill(name="builtin:...")` 按需加载，并校验冻结哈希。关闭插件包或单项技能后，后续加载会拒绝。原有 `.codetrail/skills/<name>/SKILL.md` 继续可用；自动学习产生的技能按当前 Goal/会话列出，只读显示试用、生效或历史状态，不恢复人工学习管理面板。

## MCP 接入状态

| 服务 | 本轮支持 | 配置与范围 |
|---|---|---|
| [Exa](https://github.com/exa-labs/exa-mcp-server) | 保留 Search MCP，默认停用 | 仅显式 `--search-backend exa-mcp`；当前GLM默认后端不会请求Exa |
| GLM Search Pro | 默认搜索 API | 复用宿主普通`BIGMODEL_API_KEY`，固定`search_pro`；不是MCP，不等于免费 |
| DuckDuckGo · 国际搜索 | 默认免费公开来源 | Lite无需Key；失败/验证时记录后转国际版Bing；不绕过验证码 |
| 本地文档 | 已内置 stdio | 只读当前工作区 README/docs；实际查询继续走原工具审批与路径策略 |
| [Context7](https://github.com/upstash/context7) | 新增只读远程客户端，默认关闭 | 固定 `https://mcp.context7.com/mcp`；可选本地 `CONTEXT7_API_KEY`；仅 `resolve-library-id` / `query-docs` |
| [GitHub MCP](https://github.com/github/github-mcp-server) | 新增只读远程客户端，默认关闭 | 固定官方端点；需本地 `GITHUB_PERSONAL_ACCESS_TOKEN`；仅查仓库和文件，不继承 Git 登录或开放写操作 |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | 配置模板 | 尚未接通本地第三方进程后端；不能启用，不自动执行 npx；模板版本应按上游文档审核固定 |
| [Microsoft Learn](https://learn.microsoft.com/en-us/training/support/mcp) | 默认关闭，匿名只读官方服务 | 文档搜索、代码示例搜索、仅限`https://learn.microsoft.com`的文档读取 |
| [DeepWiki](https://docs.devin.ai/work-with-devin/deepwiki-mcp) | 默认关闭，匿名公开仓库服务 | `read_wiki_structure` / `read_wiki_contents` / `ask_wiki_question`，仅单个`owner/repository`参数 |

原有文档工具 `mcp_docs` 与 `mcp_call` 都产出运行记录。托管MCP的调用参数采用固定白名单，不支持任意MCP URL、Shell或安装命令。远程返回的instructions不采纳；工具正文是不可信资料，不能提高权限、批准任务或充当本地文件验证。GitHub的访问范围受其令牌权限决定，用户应使用所需最小范围的令牌。Microsoft Learn、DeepWiki属于托管服务，按其服务条款使用，不宣称服务代码均开源。

外网通道继承“开放联网”的选择；HTTPS 校验与公网地址检查开启，不跟随重定向，不自动重试。Hosted MCP 每次操作总截止为 12 秒、单次网络等待最多 5 秒，JSON/SSE 响应上限 512 KiB，工具文本截为 16,000 字符并标记截断。同一执行上下文复用协商结果和会话，减少重复握手；不同 Agent 上下文不共享业务资料。Exa/本地文档探测限 8 秒。系统 DNS 解析仍由操作系统控制，严格墙钟时间也受调度影响。

MCP连接检测仅进行初始化和工具发现，不执行文档查询。两个搜索条目的检测则执行一次实际短查询，GLM会消耗账户搜索额度；打开页面和刷新目录不会检测或联网。通过说明当时可用，不等于持续在线。GitHub缺少凭据、网络关闭、后端未配置和模板各有独立状态。凭据只从服务器本地环境读取；不放在网页、配置模板、审计请求或Git中。

## 标识与浏览体验

GoalTree自研插件、技能和本地文档使用原创“分支树＋目标节点”标识，网页侧栏与favicon一致。Superpowers使用GoalTree设计的“能力火花＋工具”集成标识，替代作者头像，不宣称为上游官方Logo。其他第三方组件采用项目官网图标或提供方公开组织头像，按[图标来源说明](../pico/web_assets/icons/NOTICE.md)保留归属。素材随包本地加载，打开目录不向第三方传递用户IP或工作区信息；加载失败时显示名称首字。品牌素材不因源代码开源而自动取得MIT许可。

目录支持搜索及“仅看已启用”，预览技能不等于启用；切换页签后迟到的预览响应不会盖掉当前列表。

## 工作区与权限

`.pico/extensions.json` 只允许已知组件的布尔开关和修订号；乐观并发冲突返回 409，运行期间禁止修改。请求绑定原工作区和 CSRF，且在变更前验证 Goal/会话匹配。配置纳入策略指纹，旧执行上下文不能静默沿用已变化的权限配置。敏感目录仍是 Agent 不可直接访问的内部状态。

`.pico/extension-checks.json` 保存脱敏检测结果。上述文件默认忽略，不包含 API Key。这里提供的是一个小型内置扩展目录，不是通用插件市场，也不是完整 MCP 管理器；不支持自定义服务器、OAuth 动态注册、执行第三方脚本或从网页安装插件。

## 可复现验证

```powershell
python -X utf8 -m pytest tests/test_extension_hub.py tests/test_workbench_profile.py tests/test_search_mcp.py tests/test_web_frontend.py -q
node --test tests/web_hub.test.cjs
python -X utf8 scripts/run_goaltree_glm.py --workspace artifacts/my-goaltree-workspace --port 8773
```

无模型 Key 可使用离线网页演示验收扩展界面和技能加载。没有开放联网的演示不宣称能调用远程服务。真实浏览器操作、自动化回归和第三方网络连通性分别记录，均不计入既有代码修复 Benchmark。

[初版验收记录（2026-11-10）](extensions-validation-2026-11-10.md)保持当时760项回归与59项前端行为断言口径；[搜索与扩展目录更新验收（2026-11-11）](search-catalog-validation-2026-11-11.md)另记本轮实际查询、目录扩展、图标、打包及截图限制。
