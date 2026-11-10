# Search MCP

GLM入口默认`--search-backend exa-mcp`，固定连接Exa官方`https://mcp.exa.ai/mcp`。无需额外API Key，不修改Codex插件或用户全局MCP设置。它使用Exa检索，不保证等同于Bing、百度或微信索引。匿名服务有速率/可用性限制，429、认证拒绝、超时和协议错误均明确记录，无自动重试或静默网页回退。

```powershell
# 真实GLM网页入口（本地.env.glm.local中已有普通API Key）
python -X utf8 scripts/run_goaltree_glm.py --workspace <目录> --port 8773 --search-backend exa-mcp
# 通用入口显式启用
python -X utf8 -m goaltree.web --cwd <目录> --harness-profile workbench --web-access --search-backend exa-mcp
# 使用旧公开搜索页适配器
python -X utf8 scripts/run_goaltree_glm.py --workspace <目录> --search-backend public
```

`web_search(query=..., source=auto)`根据宿主配置走MCP；`source=mcp`明确要求MCP，未配置时拒绝，不换源。Bing、百度、微信等参数继续表示那些真实来源，`compare`仍表示旧三源对照，不把Exa结果混入或改名。`--no-web-access`关闭整个公开联网通道。

## 生命周期与效率

遵循2025-06-18协议的Streamable HTTP子集：初始化、initialized通知、工具列表、调用；支持JSON和有界SSE响应、可选会话头。服务器instructions不作为系统指令；只从工具列表校验固定`web_search_exa`的输入schema，兼容query、numResults和当前服务要求的objective，不调用其他工具。schema未知必需参数/协议变化即失败，而非猜参或授予能力。

启动时用独立最多15秒预热（初始化/通知/列表，一般3次HTTP请求），不伪装为用户查询耗时；不执行搜索或调用模型。宿主多个工作区共用只含协议/schema的连接状态，Goal、Task和记忆不发送到MCP。每个Agent的缓存/证据仍按原工作区与会话隔离。首个冷查询若需要重新握手，其请求和时间计入当轮原预算，可能因预算不足失败；没有无限保活线程。

已预热时，通常一次MCP查询只发一次tools/call。仍受2次搜索、3张新页、8次网络尝试、20秒累计网络阶段时间、单阶段8秒约束。GLM入口网页工具输出预算为每轮120,000字符、每次12,000字符（普通Workbench默认每轮10,000字符），单位不是Token。DNS与阻塞Socket不构成硬实时保证，GLM生成时间另计。新轮原生对话只额外携带当前工作区/会话最近3条网页URL、快照ID与获取时间，标明历史/不可信及过期状态；不重放整页正文或此前助手的未经验证结论，供“刚才页面”追问直接复读，不把空关键词检索解释为无历史页面。

缓存沿用15分钟、明确refresh可绕过，后端和策略版本进入缓存键，旧公共网页噪声不会成为MCP缓存命中。委派Task继续共享本轮预算。

## 安全与证据

- 固定官方HTTPS端点/443和POST路径，不接受任意URL、动态headers、认证信息或服务安装命令。不设置全局代理、不发送Cookies、模型Key或工作区环境。
- 每次解析DNS并检查公开地址，连接固定IP，TLS仍验证原主机名。禁止重定向、特殊/私网IP；单个响应512KiB，单SSE行64KiB；协议id、版本、内容类型和工具错误验证。
- 仅支持公开查询搜索。无roots、sampling、elicitation、回调命令、远程Agent或网页MCP fetch；返回链接由原`web_read`按既有安全规则读取。会话头不写入审计/页面。
- 已知凭据查询仍由原工具注册层拒绝。查询会传给Exa，故不能把私密附件、完整会话或本地文件内容直接放进查询；这不是通用敏感数据识别保证。
- 原MCP工具结果、每次HTTP/RPC方法、响应哈希/字节数与规范化来源保存到现有EvidenceStore。哈希范围是读到匹配JSON-RPC结果为止的响应字节，不宣称归档完整HTTP连接。模型预览剔除原MCP正文及内部请求清单，通过audit_evidence_id取回完整归档；失败同样归档。
- Exa highlights是提供方摘录，未当作独立完整网页核验。会议标题/年份不足以证明详细日期、届次和组织者；只静态识别iframe和脚本中的字面PDF引用，既不执行JS也不把引用当作已读文档；抽取版本更新后URL缓存重新读取，旧ID快照仍保留。内嵌PDF可用[web_read的PDF支持](pdf-reading.md)另行读取，未读时不因搜到入口宣称已读日程；扫描件/版式仍需专门验收。
- Shell/AppContainer、审批、写文件和验收规则不变。经典/离线入口默认仍不启用联网。

## 官方依据

- [Exa MCP与匿名基础搜索](https://exa.ai/docs/reference/exa-mcp)
- [MCP Streamable HTTP传输](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [MCP生命周期](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)

具体实测、失败记录与回归范围见[发布检查](releases/goaltree-2026-11-10.md)。不把一次命中或一次耗时外推为普遍效果。
