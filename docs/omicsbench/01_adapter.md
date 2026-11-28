# 01 · OmicsBench Adapter 设计

## 目标

不重写 GoalTree Agent，仅做薄适配：把官方 Runner 的逐轮协议接到 GoalTree 的
`Pico.start_run(execute_actions=False)` + `AgentRun.step()` 上。

## 复刻的官方协议（已对 `tmp/omicsbench_upstream` 源码核对）

- `Question` = 节点 DAG；`topological_order()` 用 `graphlib` 拓扑排序。
- 第 0 个节点提示 = `system` + `data` + `node`；后续节点只发 `node`（同一会话后续轮）。
- `node` 提示含：`Subtask: <id>`、instruction、答案 schema、choice/group-label/matrix 选项。
- 答案 = 严格 JSON 写到 `/workspace/answers/<output_filename>`，顶层 dict 键 = `resolved_output_key()`。

## 防泄露不变式

- `prompt_renderer` 只暴露：答案键名、类型占位符 shape（`<integer>/<string>/…`）、`parameters.choices`、group 标签、labeled-matrix 行列标签。
- 绝不暴露 ground truth 的数值/正确选项/条目；绝不渲染后续节点 instruction。
- 由 `tests/test_omicsbench_adapter.py::test_render_does_not_include_future_node` 与 `test_grouped_labels_revealed_but_items_not` 回归锁定。

## 工具执行边界

`OmicsBenchAdapter` 不自己执行工具；把 GoalTree 的 `Action`（`run_shell`/`read_file`/`write_file`）交给 `Environment.execute()`，Observation 只来自真实执行返回值。失败返回 `tool_status: error`，不伪造成功。真实运行把 `MockEnvironment` 换成容器执行器（`/data` 只读、`/workspace` 可写、任务无网络）。

## 评分边界

`classify_answer` 只做官方 `_run_grader` 的形状校验（top-level dict、键集合、primitive category），返回 `valid` 表示“可进入 grader”，**不是分数**。分数由外部官方 grader 产出，`summarize` 中缺分数记 `null`。

## 现状与阻塞

- 可运行：离线 mock 端到端（`MockEnvironment` + `FakeModelClient`）。
- 阻塞：无 Docker / Python 3.13 / Harbor，官方 Runner 原件未原地运行；真实模型 smoke test 未做。所有结果均标记为 development/public。