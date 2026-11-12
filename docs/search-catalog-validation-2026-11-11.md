# 搜索、扩展目录与标识验收（2026-11-11）

本轮属于产品集成与回归验证，不新增Benchmark任务，不更改既有修复成绩。保留先前学习、插件、Improver工作和历史失败。

## 交付范围

- 默认后端`glm-free`：GLM `search_pro`并行免费DuckDuckGo Lite，后者失败/验证/确认空时在剩余预算内转国际版Bing。根据用户最新选择，Tavily不接入、不调用；Exa保留显式后端，默认停用。
- 扩展目录12插件、13技能、9搜索/MCP条目。默认3插件包、7按需技能；新增的需求澄清、测试驱动、代码审查、会议写作、Microsoft Learn和DeepWiki按需启用。Playwright仍是配置模板，不能启用，不伪装成功接通。
- 原创“分支树＋目标节点”SVG替换字母G，侧栏、favicon、自研组件一致。第三方采用本地项目图标或明确归属的维护者/组织头像；来源见`pico/web_assets/icons/NOTICE.md`。资源随wheel打包，不热链远程图像。

## 真实搜索与MCP

所有原始结果位于本地忽略目录`artifacts/search-catalog-2026-11-11/`，不包含Key。外部文档依据及预算见[搜索说明](search-providers.md)、[扩展说明](extensions.md)。

| 验证 | 真实结果 | 证据 |
|---|---|---|
| 默认搜索独立调用 | `search_pro`与Lite均成功，包含Python官方asyncio链接 | `live-search.json` |
| 真实网页首次搜索 | 免费来源成功；GLM因空环境值覆盖而失败，保留partial及失败轨迹 | `ui-search-ev_342c06b4e4a1a93b1601382d7ae33144.json`、`browser-workspace/.pico/runs/run_20261111-132343-86ee2d/` |
| 修复后真实网页搜索 | 模型通过原生工具调用`web_search`，GLM和Lite各3条结果，最终显示中英文Python官方链接 | `corrected-final-check.json`、`ui-search-ev_8c3ac662a0fb77394de3fb73b82735a2.json`、`browser-workspace-v2/.pico/runs/run_20261111-132834-3283f9/` |
| Microsoft Learn | 真实初始化、工具发现及`microsoft_docs_search`成功，正文达16000字符上限并标记截断 | `microsoft-learn-schemas.json`、`microsoft-learn-call-1794374416494628600.json` |
| DeepWiki | 真实发现与`read_wiki_structure`成功，公开`modelcontextprotocol/python-sdk`仓库 | `deepwiki-schemas.json`、`deepwiki-call-1794374416494628600.json` |

初次直连国际来源时Lite曾超时，而操作系统网络路径可用；最终仅两个固定搜索端点采用系统代理设置，普通网页读取仍固定公网IP，TLS、无跳转、字节及时间限制不变。DuckDuckGo HTML入口曾出现验证，最终采用官方非JS Lite入口；不解验证码，不调用随机公共Searx实例。Bing RSS可能返回噪声，保留关键词匹配不足提示。GLM分别观测到约1.48秒和1.58秒的搜索响应，仅是烟雾验证样本，不能作为稳定延迟或性能提升结论。免费公开入口可能限流，不承诺持续在线。

## Tabbit真实操作

使用已安装Tabbit CLI及其实际页面，打开独立测试工作区，通过点击、输入和提交验证；没有使用后端API替代前端操作。

`toggles-and-images.json`记录：12/13/9目录数量；默认3包/7技能；启用测试驱动包后技能可用、预览，再关闭恢复；Microsoft Learn插件/连接页双向同步；Exa关闭；Playwright仅模板；刷新后配置保存；九个本地图像全部解码且`naturalWidth>0`。

`corrected-final-check.json`记录真实用户输入→模型→工具→结果展示，GLM与Lite来源分别出现。580×840视口下目录单列、12张卡片、无对话框横向溢出；恢复1520×722桌面尺寸。插件目录打开/刷新只读本地元数据，不发起探测查询。`desktop-capture.json`资源列表仅包含本地URL，无第三方图标热链。

**限制：** 原生截图等待12秒后超时（`SCREENSHOT_CAPTURE_FAILED`），未获得截图，不声明已完成像素级视觉验收。一次自动化选择器匹配两个summary，先只读观察，再收窄到实际执行摘要并成功操作；失败回执保留。首次窄屏检查遇到异步目录尚未填充，最终增加等待12张卡片后重新验证，未把空列表当作通过。

## 修复的问题

1. **GLM搜索凭据二次覆盖。** 专用启动器先从指定`.env.glm.local`读取普通Key，随后Workbench默认加载父目录`.env`的空同名值；聊天客户端持有已读取的Key，搜索却从被覆盖的环境读取，因此出现“聊天能用、GLM搜索不能用”。专用启动器改为`load_env=False`，只采用其已选配置。新增真实启动入口回归，模拟父目录空值，确认不会覆盖；不放宽敏感目录或Shell权限。
2. **目录异步预览。** 切换页签/搜索/关闭后，迟到的技能预览不能覆盖当前视图；Node行为测试验证。
3. **打包遗漏许可说明。** 增加Web资源根目录Markdown打包规则，wheel对51份静态资源、图标、技能与许可逐字节核对通过。
4. **新MCP工具名。** 根据实际工具发现使用DeepWiki的`ask_wiki_question`，保留固定读工具白名单；Microsoft文档读取仅限HTTPS官方域。未依赖陈旧文档猜测工具名。

## 自动化与复现

完整回归 **816/816通过，903.74秒**。执行期间真实网页验收发现并修复启动器配置覆盖问题，随后新增启动器回归 **1/1通过**，相关子集 **99/99通过**；按测试ID合并去重，当前收集的 **817项测试全部覆盖并通过**，不是声称一次运行了817项。前端Node **61/61通过**（其中测试脚本已由Python包装调用，不与817相加）。wheel逐字节核对 **51份资源通过**，待提交/未忽略文件扫描未发现本地配置的密钥值。

机器汇总见[checks.json](search-catalog-checks-2026-11-11.json)。本地完整结果与源文件校验位于`artifacts/search-catalog-2026-11-11/`下`test-summary.json`、`pytest.xml`、`pytest.log`、`launcher-regression.xml`、`node.log`、`package-check.json`及`final-audit.json`。321份初始源文件清单中仅三份发生预期变化：`scripts/run_goaltree_glm.py`、`tests/test_search_hybrid.py`和`pyproject.toml`，分别由新增启动回归/99项相关回归及独立wheel资源验证覆盖；其他运行代码未变。初始和最终源文件哈希均保留，不宣称回归期间源文件完全未变。

```powershell
python -X utf8 -m pytest -q
node --test tests/web_poll.test.cjs tests/web_markdown.test.cjs tests/web_controls.test.cjs tests/web_activity.test.cjs tests/web_workspace.test.cjs tests/web_tree.test.cjs tests/web_hub.test.cjs
python -X utf8 scripts/run_goaltree_glm.py --workspace artifacts/my-goaltree-workspace --port 8773 --search-backend glm-free
```

首次使用系统过旧setuptools进行`--no-build-isolation`打包得到无效UNKNOWN包，未视为通过；在本地忽略目录使用满足项目构建要求的setuptools75.8.2/wheel0.45.1后，构建`goaltree_agent-0.1.0-py3-none-any.whl`并完成51份资源核对。未修改全局Python安装、未关闭TLS验证，初始失败产物保留。

独立预览服务为`http://127.0.0.1:8778/`，模型`glm-5.3-flash`、AppContainer隔离、`glm-free`后端。旧8773服务的停止/重启被自动审批以“blocked by policy”拒绝，因此保留旧服务并使用独立端口，未绕过该拒绝。测试工作区与用户原有工作区分离，原会话和历史评测未覆盖。
