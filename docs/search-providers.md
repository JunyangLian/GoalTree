# 默认搜索：GLM Search Pro 与免费国际来源

GoalTree GLM启动脚本与Web CLI默认采用`--search-backend glm-free`。保留`public`旧来源模式与显式`exa-mcp`，不静默替换模型、引擎或服务商。默认组合为GLM `search_pro`与DuckDuckGo Lite，Tavily不接入、不读取其Key，Exa不会收到默认请求。

| 来源 | 调用方法 | 凭据与计费 | 故障行为 |
|---|---|---|---|
| GLM | POST `https://open.bigmodel.cn/api/paas/v4/web_search`，固定`search_engine=search_pro` | 本地普通`BIGMODEL_API_KEY`；使用已有账户搜索额度，实际收费以平台为准 | 保留HTTP/连接/schema错误，不切换其他GLM搜索引擎 |
| DuckDuckGo | `https://lite.duckduckgo.com/lite/`，`kl=us-en` | 免费公开非JS入口，无额外账户或Key | 自动组合中遇到验证、失败或确认空结果时转国际版Bing，保留原尝试 |
| 国际版Bing | 公开RSS `mkt=en-US&setlang=en-US` | 免费公开入口，无Key | 失败即记录，不再扩散其他来源；结果可能与浏览器不同 |

接口依据：[智谱网络搜索](https://docs.bigmodel.cn/cn/guide/tools/web-search)、[智谱API参数](https://docs.bigmodel.cn/api-reference/工具-api/网络搜索)、[DuckDuckGo非JavaScript入口](https://duckduckgo.com/duckduckgo-help-pages/features/non-javascript)。公开搜索源会限流或要求验证，不保证永久免费或持续可用，不解验证码。

## 效率与证据

`source=auto`并行调用GLM和免费分支；Lite先用最多4秒，剩余总预算允许时再转Bing。搜索操作的总网络截止仍是8秒；每轮最多2次搜索、3个新页面、8次网络尝试、20秒累计网络时间。没有自动重试。系统DNS由操作系统管理，因此这些是协作式截止而非硬实时保证。

按URL规范化去重，以RRF合并来源排名；排名分数不代表事实可信度。每条结果标明实际来源，每次尝试分别记录成功、确认空、验证、连接故障或协议错误，部分失败不被改写为“没有相关官方资料”。GLM审计保存请求/响应SHA256、状态及字节数，不保存Authorization。搜索摘要是线索，重要结论仍需读取原页面核对。

会话缓存15分钟，按工作区、会话、查询、来源及启停状态隔离；失败/部分失败不缓存为成功。`refresh=1`强制更新。禁用GLM或国际来源不会继续使用其缓存；两者都关闭则拒绝默认搜索。显式`source=duckduckgo`不自动转Bing，`source=glm`要求当前配置GLM后端，避免伪造来源。

## 网络与隐私边界

GLM使用原有公网地址校验、固定IP连接和TLS主机验证。仅两个固定国际搜索端点允许使用操作系统现有网络代理设置（本机直连Lite曾超时，系统网络路径可用）；不向页面开放自定义代理、Cookie、认证头或任意URL，不跟随重定向，不关闭TLS校验，不绕过验证。普通`web_read`继续走原有公网校验和固定IP读取路径，Shell/AppContainer权限不变。

查询会发送到所选搜索服务，因此不要把私有文档、密钥或敏感内容拼入查询。服务端凭据不进入浏览器；图标均本地加载。托管MCP查询也只应传公开查询或公开仓库名，不能把接口“只读”误解为可以发送私有资料。

## 运行

```powershell
python -X utf8 scripts/run_goaltree_glm.py --workspace artifacts/my-goaltree-workspace --port 8773 --search-backend glm-free
python -X utf8 -m pytest tests/test_search_hybrid.py tests/test_web_search.py tests/test_search_mcp.py tests/test_extension_hub.py tests/test_web_frontend.py -q
```

Key仅填本地忽略文件`.env.glm.local`的`BIGMODEL_API_KEY`或宿主环境。没有配置GLM时，免费分支仍可独立返回结果，但必须披露GLM缺少凭据，不把部分来源成功当作两个服务全部成功。启动、打开目录和刷新不会自动进行收费查询。
