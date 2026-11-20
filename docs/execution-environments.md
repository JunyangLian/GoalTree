# 外部执行环境与双通道工具契约

本文描述 `EnvironmentProfile` 和显式 `wire_protocol`。它们解决的是：同一个 Agent 在本地仓库、模拟 App 或问答环境中运行时，文本工具协议与原生 Tool Calling 不再各自继承不同的角色、参数类型和预算说明。

本次没有新增外部 Benchmark 成功率，也没有实现 step-wise API、事件队列、自动单位判真或强制写操作。

## 角色与协议

```python
from pico import EnvironmentProfile, Pico

profile = EnvironmentProfile(
    name="simulated-phone",
    role="You are GoalTree, operating a simulated phone through its applications.",
    instructions=(
        "There is no host file system.",
        "Finish required environment actions before reporting completion.",
    ),
    budget_instruction="Reserve a step for the required environment action.",
)

agent = Pico(
    client, workspace, session_store,
    environment=profile,
    wire_protocol="auto",  # auto / text / native
)
```

- `auto` 保留原行为：客户端明确支持原生工具时使用原生协议，否则使用文本协议。
- `text` 强制 `<tool>...</tool>` 文本协议，并清除可能复用的旧 native conversation。
- `native` 强制原生 Tool Calling；客户端不支持时在发起请求前明确拒绝。
- 实际选择保存在 `requested_wire_protocol` 和 `effective_wire_protocol`，并进入 run trace/report。`auto` 是请求配置，不应被当作实际线上协议。

显式 environment 只提供角色、任务说明和预算语义。文本与原生渲染器分别添加自己的协议规则；不会把 XML 示例拼进原生 System，也不会在外部文本环境中展示不存在的 `read_file/write_file/run_shell` 示例。

Goal 协调器和宿主绑定的 Goal Executor 仍优先使用既有宿主角色。新 environment 不会覆盖 GoalStore、任务规格、依赖或验收权限。

## 工具 Schema

旧声明仍可正常使用：

```python
{
    "schema": {
        "recipients": "list[str]",
        "attendees": "list[int]=[]",
        "dry_run": "bool=False",
        "score": "float=1.0",
        "metadata": "dict={}",
        "subject": "str",
    },
    "risky": False,
    "effect": "write",
    "description": "Send an email in the simulated environment.",
    "run": send_email,
}
```

文本和原生现在共用同一个对象 JSON Schema。支持 `string/integer/number/boolean/array/object/null`、数组元素类型、required/default、嵌套 properties、additionalProperties、enum 及常用长度/数量/取值限制。需要嵌套或联合类型时可直接提供：

```python
{
    "json_schema": {
        "type": "object",
        "properties": {
            "message": {
                "type": "object",
                "properties": {
                    "recipients": {"type": "array", "items": {"type": "string"}, "minItems": 1}
                },
                "required": ["recipients"],
                "additionalProperties": False,
            }
        },
        "required": ["message"],
        "additionalProperties": False,
    },
    # risky / effect / description / run ...
}
```

`json_schema` 是执行时的权威结构声明；旧 `schema` 可保留作文本展示/兼容信息，但不会再把 nullable 或 Schema 独有字段拒绝掉。只有旧声明的工具保持原行为。文本 prefix、工具签名和原生 payload 都支持仅提供 `json_schema` 的工具。

外部工具在 classic profile 也会按相同 Schema 校验；非法参数在执行器调用 `run` 之前拒绝。Workbench 在新 Schema 存在时跳过旧字符串结构校验，但 Goal 契约、权限、policy hook 和专门内容检查仍然执行。内置本地工具继续沿用原有专门的路径、权限和内容校验。Schema 校验不证明参数语义正确，也不替代审批。

旧文本 client 不需要声明新的 native 属性。旧原生 client 若不能接受 environment 关键字，会在请求前得到明确的能力错误，而不是静默退回 coding System 或抛出难定位的关键字错误。

## RunOutcome：资源耗尽不等于任务完成

`status` 与 `stop_reason` 保留原有进程/协议兼容语义；新的 `resource_exhausted` 和 `task_outcome` 单独进入 TaskState、report、trace 和运行汇总：

- `resource_exhausted`：空值表示未观察到该资源耗尽；`tool_budget` 表示已进入工具预算耗尽的 finalization。
- `task_outcome=completed`：宿主接受了最终答复，且本次启用的独立验证已通过。它不是外部 Benchmark judge 或人工验收的替代。
- `task_outcome=unknown`：已有最终文本，但没有足够独立证据确认任务完成。没有验证通过证据的正常答复使用该值；预算耗尽仍未取得该证据时同样如此，不能仅凭模型自述改成 completed。
- `task_outcome=incomplete`：没有获得可接受的最终答复或运行提前停止。
- `task_outcome=failed`：模型请求等运行错误导致失败。

即使最后一步刚好完成独立验证，`resource_exhausted=tool_budget` 也可以与 `task_outcome=completed` 同时成立。相反，旧 `status=completed/final_answer_returned` 只说明协议正常收尾，不能单独证明任务完成。

## 输出与效果观测

工具轨迹新增：

- `output_total_chars`：脱敏后完整输出的字符数，也是预览、存储和 offset 共用的坐标；
- `output_raw_chars`：脱敏前原始输出的字符数，仅作审计指标；
- `output_preview_chars`：最终模型可见内容的字符数，包含 Workbench 追加的证据后缀；
- `output_truncated`：原始输出是否超过预览上限；
- `output_limit_chars`：本次预览上限；
- `effect`：工具声明存在时原样记录，例如 read/write/submit/wait/unknown；未知效果不得默认当成只读。

工具可用可选 `output_limit` 调整自身预览上限。发生截断时，classic 外部环境还会得到 `output_id`、`output_next_offset`、`output_stored_chars` 和 `output_stored_truncated`，并通过宿主工具精确续读：

```text
read_tool_output(output_id="out_...", offset=4001, limit=4000)
```

返回 `text/next_offset/has_more/total_chars/stored_chars/stored_truncated`。offset 从 1 开始按字符计数，因此一整行超长 JSON 也能准确续读；单次 limit 最大 12000。宿主分页说明置于预览开头，避免文本历史的二次裁剪把它丢掉。仅在发生截断时保存输出；运行时先脱敏完整文本，再以同一份文本生成预览、存储和 offset，避免脱敏长度变化造成重复或丢失。每个会话最多 40 份，每份最多 131072 字符。超过存储上限或会话份数时会明确保留缺口，不静默淘汰旧证据。`read_tool_output` 只读当前会话的宿主审计数据，不执行环境动作，也受 allowed_tools 限制。

默认本地 coding 不注入该工具；Workbench 继续使用原有 EvidenceStore，不重复保存。外部环境的 allowed_tools 可以使用当前注册表中的工具名，未注册名称仍被拒绝。

## 边界与验收

- 不把“发生一次写调用”当作任务完成；写尝试、成功副作用和外部 judge 认可必须分别统计。
- 不在预算不足时强迫模型盲写，也不自动把 `17000` 改成 `17`。格式拒绝与语义判真是不同问题。
- 目前仍是整段 `ask()` 控制循环。多轮环境事件、Exgentic 逐轮 Action/Observation 和运行中取消语义需要后续 step-wise 状态机。
- 默认本地 coding 和 GoalTree 宿主路径不改变；只有显式传入新配置才启用上述外部环境行为。

离线验证：

```powershell
python -X utf8 -m pytest tests/test_environment.py tests/test_output_store.py tests/test_task_state.py tests/test_agent_loop.py tests/test_native_tools.py tests/test_prompt_prefix.py tests/test_response_contract.py -q
```

这些测试检查实际 wire 的角色、Schema、预算说明和 typed arguments，不调用模型，也不代表真实模型任务成功率。

2026-11-20 验证结果：当前源码全量 **1101/1101 通过**（1065.40 秒），其中环境/wire 合同 12 项、输出存储与自主分页合同 6 项，RunOutcome 相关回归亦纳入全量套件；外部评测适配器独立测试 **14/14 通过**。隔离 PEP 517 wheel 构建成功，并确认包含 `pico/environment.py` 与 `pico/output_store.py`。静态检查工具 ruff 在当前环境未安装，因此没有声称执行 ruff。
