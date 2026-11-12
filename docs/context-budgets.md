# 上下文与文档预算

## 单位与配置

### 2026-11-12：GLM默认入口的1M Token配置

智谱[官方规格](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash.md)列出`glm-5.3-flash`支持1M上下文；官方模型配置的`max_position_embeddings`为1,048,576。当前GLM入口将Token窗口与物理请求大小分开：

| 限制 | 当前默认 | 单位与用途 |
|---|---:|---|
| `--context-tokens` | 1,048,576 | 输入与输出总窗口 |
| `--input-tokens` | 1,032,192 | 输入Token本地估算上限，预留输出/模板余量 |
| `--native-input-budget` | 33,554,432 | 独立的序列化JSON字节上限，不是Token |
| 最大新输出 | 8,192 | 请求Token参数；仍从总窗口预留 |

使用官方`zai-org/GLM-5.3-Flash`固定提交`46a050bec13002ad29ee795c16e61cf4fa3c4a9c`的本地tokenizer，校验文件SHA256；不加载权重、不执行远程代码，不上传文本去计算Token。计量对象是含消息、工具与已有reasoning的聊天JSON，额外预留4096＋1%安全余量；**这仍是本地估算，不等于服务商模板的精确计数**。服务商`usage`单独记录，不把估算写成实耗。

```powershell
python -m pip install -e ".[attachments,web-pdf,long-context]"
python -X utf8 scripts/run_goaltree_glm.py --prepare-context-tokenizer --check-config
python -X utf8 scripts/run_goaltree_glm.py --workspace artifacts/my-workspace --port 8773
```

首次显式准备只下载约20MB的固定官方tokenizer文件，缓存到本机用户`GoalTree/tokenizers/glm-5.3-flash`目录。后续启动不自动联网下载；缺少依赖、文件或哈希不符明确拒绝启动，不静默退回旧字节预算或其他模型。`/api/config`分别暴露`native_counting`、`input_token_budget`、`token_context_window`。不同工作区沿用同一宿主配置。

1M是容量上限，不代表每轮填满、不等于自动记忆或所有附件都可输入1M。单消息、附件、PDF、工具输出、隐私检查仍有各自的有限资源边界；未检查图片仍拒绝外发。输入超过模型/资源预算、无法检查或不能保留当前必要约束时仍失败关闭。

### 长任务结构治理

当前Goal/Task状态只在Host状态帧出现一次；continuity中已完全对应现有契约的副本改为位置引用。当前约束、协作设置、附件与版本信息保留，不截断成“摘要”。旧产出预览不再重复进入保护帧，保留实际证据ID按需查回。

workbench历史用户请求不再无界拼到当前请求的保护帧。它们保存在原会话与私有证据中，带顺序及会话内证据引用，作为可压缩的历史组；当前请求和当前契约优先，旧记录不代表新权限。摘要省略时仍可查回完整原文。classic兼容入口保持原有历史策略。这里没有语义总结模型，不能保证任意隐含的旧要求都被自动归纳为当前契约。

最近最多3份、每份600字符的同会话助手产出可作为可压缩历史证据恢复；不视为用户指令或验收。压缩读取记录保留实际返回行区间和源哈希，不把`end=200`误当作实际返回200行，也不把短预览当作未读全文。历史问题与运行时纠正区分优先级；必要内容仍需查回，文件变化后仍需刷新。这些是进度与可恢复性机制，最新真实小预算复测仍出现重复读取，不能据此宣称模型总能正确使用摘要。

隐私检查的有限字符上限调整为16,777,216以适配长请求；超限继续拒绝，域名、等级、来源继承、审批与执行权限不变。邮箱检测改为围绕实际`@`扫描，避免长无`@`字符串触发二次复杂度；长正文尾部凭据/邮箱仍有回归。完整测试与真实容量结果另见本轮报告。

### 历史字节模式（保留兼容）

`scripts/run_goaltree_glm.py`使用普通GLM API，模型保持`glm-5.3-flash`。预算是上限，不会提前填充对应长度的输入。

|限制|更早GLM入口|此前GLM入口|单位|
|---|---:|---:|---|
|本地原生输入估算上限|131072|983040|JSON序列化UTF-8字节估算|
|本地上下文估算天花板|262144|1048576|JSON序列化UTF-8字节估算|
|每次模型最大新输出|8192|8192|请求Token参数|
|每轮网页工具累计输出|10000|120000|字符，包含JSON元数据|
|单次网页工具输出|3800|12000|字符，包含JSON元数据|
|每次读取最大行数|20|120|清洗后快照行|

这些值不是同一单位。历史`NativeContextBudget`按包含Unicode转义的JSON字节保守估算；字节模式中扣除请求输出Token仍是兼容的近似预留，不能当成精确Token边界。此前1MiB估算天花板**不等于已验证可以输入1M Token**。未传入Token计量器的通用Workbench、classic及旧评测继续沿用该模式；新GLM入口见上节。

大输入仍会增加延迟与费用，不把整个历史、全部仓库或完整PDF无条件注入。定点问题用查找/页码读取，用户要求通读时才按next_start连续分段。原生调用与对应结果保持成组，压缩不拆协议。

```powershell
python -X utf8 scripts/run_goaltree_glm.py --workspace artifacts/my-goaltree-workspace --port 8773 --network-route direct --search-backend glm-free --context-tokens 1048576 --input-tokens 1032192
```

`/api/config`暴露`native_counting`和`web_output_limits`，新工作区继承主机配置。普通Workbench及历史评测默认不变。每个新回合新建网页配额，同一次父子任务共享配额。

## 故障证据

曾发生网页工具输出配额耗尽，被旧错误文案误称为模型上下文不足。现已明确区分工具字符配额和模型输入预算；该类错误不代表模型窗口已满。

另修正通用工具执行器二次4000字符裁剪：网页结果已经在生成时按边界裁剪为完整JSON，执行器遵循该实例的单次结果上限，不再在中间截断行/JSON/引用。

原2次搜索、3个新资源、8次网络尝试、20秒累计网络阶段时间及AppContainer、审批、工作区和PDF解析资源限制保持不变。输出额度变大不授予额外联网/执行权限。

功能检查范围见[发布说明](releases/goaltree-2026-11-10.md)，不计入历史Benchmark。

百万输入的真实usage、三处标记观测、原12轮序列的全部批次及本地SSE回调修复，见[本轮报告](long-context-1m-2026-11-12.md)和[机器汇总](long-context-checks-2026-11-12.json)。网络读取与本地回调耗时分开记录；本轮已发现整份历史脱敏/持久化拖慢SSE的案例，不能把所有超时归因于模型服务。
