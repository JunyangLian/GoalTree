# 运行时一致性更正（2026-11-07）

本更正适用于 `glm-repair30-v2-2026-11-06` 与 `glm-repair12-compat-v3-2026-11-06`，不改写冻结资产、原始失败或历史分数。

## 发现及影响

`Pico.__init__` 会调用 `build_tools()`。`pico.tools.build_tool_registry(context)` 使用 `functools.partial` 捕获当时的 `ToolContext`，其中 `isolated_shell_runner` 已绑定到原 `Pico._isolated_shell`。评测适配器随后才执行 `agent._isolated_shell = ...`，没有改变工具闭包已经保存的回调。

因此，旧代码中：

- CodeTrail Agent 内部 `run_shell` 使用原生 AppContainer 测试执行器，继承 Agent 的 Python 3.13.11。
- ApeCode 的工具测试通过指定 `PinnedTestExecutor`，使用固定哈希 Python 3.10.1。
- 两方第一次、第二次独立外部验收均通过 `PinnedTestExecutor`，使用统一 Python 3.10.1；这部分没有被旧回调影响。
- 两方均保持 AppContainer 边界，没有变成宿主执行或放宽 ACL。问题是运行时控制失效，而非工具权限扩大。

最初做过的30题故障/参考资格只覆盖指定外部执行器，未证明 CodeTrail 实际工具注册表也调用了它。`controlled-checks.json` 的配置/文件相等以及工具记录中的 `execution_backend=appcontainer` 不能证明具体解释器一致；原报告关于“两 Agent 所有测试统一3.10”的表述不成立。

增强原有集成测试后，令原生回调抛出哨兵异常，并记录给定执行器的实际调用：旧实现**0次调用**，预期修改前/后共2次，确定失败。见[红测JUnit](glm-callback-red-2026-11-07.xml)。这一检查没有请求模型或执行宿主测试。

## 如何处理旧结果

**解释修订：内部Python不同不是系统横评的禁止条件。** 评测目标是系统效果时，可保留各自实现和运行时，统一可信外部验收并披露资源/权限差异。这里需要撤回的是“所有内部测试统一”的不实配置声明，不是全部结果。前版将其概括成不能横评过严，已收窄为不能声称符合旧统一运行时协议或识别单因素因果收益。参见[系统评测方法](../../docs/system-evaluation-method.md)及[多维结果](../system-comparison-v2-2026-11-07/report.md)。

47次实际尝试、所有请求、工具、失败和预算账本全部保留。统一外部验收支持原最终文件功能通过/失败的事实；流程率、耗时和成本则必须附内部解释器不一致的限制。

汇总报告保留原分数，可用于本配置的系统描述性比较；不声称执行符合旧统一运行时协议，不发布泛化优越性或单模块归因结论。不将已有通过自动变成新版本成绩，不补跑失败，不把已启动预算中止排除；未启动任务继续未知。

## 修正与验证

修正在工具注册前绑定指定执行器，使 `ToolContext` 首次捕获的就是受控回调；运行时保持固定命令、原路径策略、AppContainer 证明和无宿主回退。

同一增强集成测试必须确认修改前/后测试实际调用给定执行器，且原生回调从未调用。另用 Python3.13 Agent 和 Python3.10 测试后端做真实内核专项探针：由离线脚本决策触发工具，真实执行创建临时目录并写文件的测试，核对桥接解释器与 AppContainer 审计。它仅验证接线，不计入修复任务或模型成绩。

[47项相关回归](glm-callback-fixed-targeted-2026-11-07.xml)通过；[真实内核专项](runtime-qualification.json)确认Python3.13 Agent实际调用指定Python3.10桥接、AppContainer内临时目录写入通过、原生旧回调未执行，模型调用0次。最初两版专项探针缺少过程统计元数据（expected_tests、sha256），已保留其原目录与后续合格版，不隐藏探针搭建错误。最终固定源码[全量474/474通过](glm-callback-final-full-v2-2026-11-07.xml)，结束时源码仍匹配运行前哈希；[完整测试历史](test-history.json)保留红测和各次运行，不累计重叠测试数。真实模型修正后复测**未运行**：先前批准的2M Token预算已触发保护，不能借账户20M可用额度自动另开实验。本更正没有声称优化后横评成功率。
