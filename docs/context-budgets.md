# 上下文与文档预算

## 单位与配置

`scripts/run_goaltree_glm.py`使用普通GLM API，模型保持`glm-5.3-flash`。预算是上限，不会提前填充对应长度的输入。

|限制|原GLM入口|当前GLM入口|单位|
|---|---:|---:|---|
|本地原生输入估算上限|131072|983040|JSON序列化UTF-8字节估算|
|本地上下文估算天花板|262144|1048576|JSON序列化UTF-8字节估算|
|每次模型最大新输出|8192|8192|请求Token参数|
|每轮网页工具累计输出|10000|120000|字符，包含JSON元数据|
|单次网页工具输出|3800|12000|字符，包含JSON元数据|
|每次读取最大行数|20|120|清洗后快照行|

这些值不是同一单位。`NativeContextBudget`沿用包含Unicode转义的JSON字节保守估算，不使用厂商精确Tokenizer；`context_window`扣除请求的输出Token数也是现有近似预留，不能当成精确Token边界。当前1MiB估算天花板**不等于已验证可以输入1M Token**。精确输入/输出Token只引用服务商实际usage；未知值不补零。服务商长上下文能力与当前接入端点限制还需区分，不能用客户端字节配置证明服务端容量。

大输入仍会增加延迟与费用，不把整个历史、全部仓库或完整PDF无条件注入。定点问题用查找/页码读取，用户要求通读时才按next_start连续分段。原生调用与对应结果保持成组，压缩不拆协议。

```powershell
python -X utf8 scripts/run_goaltree_glm.py --workspace artifacts/my-goaltree-workspace --port 8773 --network-route direct --search-backend exa-mcp --native-input-budget 983040 --native-context-window 1048576 --web-output-chars 120000 --web-result-chars 12000 --web-read-lines 120
```

`/api/config`暴露`native_counting`和`web_output_limits`，新工作区继承主机配置。普通Workbench及历史评测默认不变。每个新回合新建网页配额，同一次父子任务共享配额。

## 故障证据

曾发生网页工具输出配额耗尽，被旧错误文案误称为模型上下文不足。现已明确区分工具字符配额和模型输入预算；该类错误不代表模型窗口已满。

另修正通用工具执行器二次4000字符裁剪：网页结果已经在生成时按边界裁剪为完整JSON，执行器遵循该实例的单次结果上限，不再在中间截断行/JSON/引用。

原2次搜索、3个新资源、8次网络尝试、20秒累计网络阶段时间及AppContainer、审批、工作区和PDF解析资源限制保持不变。输出额度变大不授予额外联网/执行权限。

功能检查范围见[发布说明](releases/goaltree-2026-11-10.md)，不计入历史Benchmark。
