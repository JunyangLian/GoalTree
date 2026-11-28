# 07 · Agent 问题清单与修复记录（诊断 + 改进）

> 边界：本清单来自「用 OmicsBench 公开任务（bovine）当诊断器」的过程，环境为真实模型（GLM 5.3-flash）+ 真实数据 + Windows HostEnvironment（无 Docker）。**全部为本地开发诊断，非官方 OmicsBench 成绩，不用于对比或宣称优于其他 agent。**
>
> 目标：找出并修复让 agent「算对了却拿不到分 / 跑不稳」的问题，而非刷分。

## 0. 总览

| # | 问题 | 类型 | 状态 |
| --- | --- | --- | --- |
| 1 | 裸 `/data` 路径未映射 | 真实 bug | ✅ 已修 |
| 2 | Linux 命令摩擦（zcat/ls/heredoc） | 环境适配 | ✅ 已修 |
| 3 | `write_file` 空参数 / 不落盘 | agent/协议 | ✅ 已修 |
| 4 | 内容对、格式错（schema_mismatch） | agent/协议 | ✅ 已修（正向信号，n 小） |
| 5 | node1 差分表达「算得对但不收尾/超时」 | 模型层 | ⚠️ harness 补不动 |
| 6 | 方法漂移被检出、但 recovery 尚未纠正成功 | 科学管线 | ✅ 机制已修 + 离线验证（真机待 #5） |
| 7 | schema_mismatch 恢复提示不含「实际键 vs 期望键」diff | agent/协议 | ✅ 已修 + 离线验证 |
| 8 | experiment 未回填 token/成本（usage 缺失） | 观测/度量 | ✅ 已修 + 真机验证 |
| 9 | 跨节点 tool_calls / 验证观察范围污染（共享 env 累积） | 真实 bug | ✅ 已修 + 离线验证 |
| 10 | 复用 work_root 时陈旧答案被当成本次答案 | 真实 bug | ✅ 已修 + 离线验证 |
| 11 | `previous_retried` 恒 False，「上一步失败」提示是死代码 | 真实 bug | ✅ 已修 + 离线验证 |
| 12 | 模型/传输异常直接抛出，会中断整轮 k×config 扫描 | 真实 bug（稳定性） | ✅ 已修 + 离线验证 |
| 13 | 同一 step 内多个 action：答案已 valid 后仍执行后续 action 可覆盖好答案 | 真实 bug | ✅ 已修 + 离线验证 |
| 14 | 与官方 renderer 逐函数一致性核对 | 验证 | ✅ 已核对（无差异） |
| 15 | 单节点异常（env/渲染/验证）中断整轮 | 真实 bug（稳定性） | ✅ 已修 + 离线验证 |
| 16 | wall-timeout 只在回合顶部检查，慢 step 带出一整批 action | 真实 bug | ✅ 已修（在途请求仍不可抢占） |
| 17 | 瞬态模型异常被当成超时，吞掉恢复预算 | 真实 bug | ✅ 已修 + 离线验证 |
| 18 | max_rounds 用尽时 run 未关闭 | 真实 bug | ✅ 已修 + 离线验证 |
| 19 | 答案解析比官方宽松（重复键 / NaN 判 valid） | 真实 bug | ✅ 已修 + 离线验证 |
| 20 | 报告不诚实（失败 run 不可见 / timeout 作用域含糊） | 观测/度量 | ✅ 已修 |
| 21 | dev 明细无法区分「选错」与「菜单外」 | 观测/度量 | ✅ 已修 |
| 22 | run_shell 命令超时可远超 wall 预算 | 真实 bug | ✅ 已修 |
| 23 | 中断 / 崩溃丢整份 report | 稳定性 | ✅ 已修 + 真机验证 |
| 24 | multiple_choice 菜单外值无纠正 | agent/协议 | ✅ 已修 + 真机复现（expR32） |
| 25 | 脚本内容器路径未翻译（node1 全崩在环境探测） | 环境适配 | ✅ 已修 + 真机验证 |
| 26 | 漂移判定过宽（打印 padj 也判 blocking） | 科学管线 | ✅ 已修 + 真机验证 |
| 27 | 首次尝试吃光 wall 预算，恢复零秒可用 | 真实 bug | ✅ 已修 + 真机验证 |
| 28 | schema_mismatch 恢复提示不含结构模板 | agent/协议 | ✅ 已修 |
| 29 | 科学约束闸门接入核心 pico 主循环 | 核心 | ✅ 已接入（全量 1201 通过） |
| 30 | 题面诱饵（两版）未能诱发 padj 漂移 | 验证（负结果） | ⚠️ 该模型遵循很强，诱饵不成立 |
| 31 | 失败命令被当成科学证据（假阳性） | 真实 bug | ✅ 已修 + 离线验证 |
| 32 | 纠偏后改用表格预计算 pvalue（假通过） | 科学管线 | ✅ 提示增强（部分有效） |
| 33 | 受控注入验证漂移闭环 | 验证 | ✅ 检出 5/5、内容改对 3/5 |
| 34 | 证据只读命令文本，脚本内分析看不见（漏报） | 科学管线 | ✅ 已修（脚本被执行才计入） |
| 35 | 纠偏未说明「怎么算」，模型可换口径 | 科学管线 | ✅ 已修（引用指令原句） |
| 36 | 换用表格预计算列仍判 PASS（假通过） | 科学管线 | ✅ 已修（V4 重算证据，离线验证） |
| 37 | 纠偏回合被「旧的有效答案」提前终止（recovery 空转） | 真实 bug | ✅ 已修 + 离线验证 |
| 38 | 只纠正方法时不重申答案路径，模型写到自拟文件 | agent/协议 | ✅ 已修 + 真机复现 |
| 39 | 纠偏后改写分组标签（`up_in_IMF`），等价于零分 | 科学管线 | ✅ 已修（V5 标签校验 + 离线验证） |
| 40 | 纠偏回合缺「schema + 标签」模板 | agent/协议 | ✅ 已修 + expR29 5/5 |
| 41 | 多选答案含「菜单外基因」未被纠正 | agent/协议 | ✅ 已修（离线验证；真机未复现触发） |
| 42 | 数值型答案的统计口径无法从文本证据反推 | 科学管线（边界） | ⚠️ 已确认、不修（记录为已知边界） |
| 43 | 答案已写在回复里、只提示「写文件」导致重算 | agent/协议 | ✅ 已修 + 离线验证 |
| 44 | Windows 宿主垫片导致「非容器条件」 | 环境 | ✅ 已加 Docker 后端（自建轻量镜像，非官方镜像） |
| 45 | 嵌套形状写坏（`items` 写成空格串） | agent/协议 | ✅ 已加交付纠偏 + 真机验证（expD6：5/5 形状修复） |
| 46 | 记录型答案写成裸字符串（`differential_feature_set`） | agent/协议 | ✅ 已加交付纠偏 + 真机验证（expD12：3/3 形状修复） |
| 47 | 数值答案写成字符串（`numeric_tolerance`） | agent/协议 | ✅ 已加交付纠偏 + 真机验证（expD14：3/3 转数字，2/3 容差内） |
| 48 | 对比度/分组定义无法从证据验证 | 科学管线（边界） | ⚠️ 已确认、不修（记录为已知边界） |

## 1. 裸 `/data` 路径未映射

- **现象**：node1 反复 `os.listdir('/data')`、`os.walk('D:/')` 全盘找 `gene_expression`。
- **根因**：`HostEnvironment._translate/_resolve` 只处理 `/data/`（带尾斜杠），裸 `/data`（无尾斜杠）不翻译 → 在 Windows 上解析到错误位置。
- **修法**：`_translate`/`_resolve` 改为同时处理裸 `/data`、`/workspace`（带不带斜杠均映射）。
- **验证**：新增 `test_host_environment_translates_bare_paths`，离线全绿。
- **证据**：exp4 trace 显示 `os.walk('D:/')` 全盘扫描。

## 2. Linux 命令摩擦

- **现象**：模型连续尝试 `zcat`/`ls`/`mkdir -p`/`cat > x << EOF`/`sed`，Windows cmd 塞不足或报 opaque 错误 → 空转。
- **修法**：`HostEnvironment._linux_hint` 对已知 Linux-only 命令与 heredoc 直接返回清晰指引（用 Python / 用 write_file）；`EnvironmentProfile.instructions` 增加「优先用 Python」提示。
- **验证**：`test_host_environment_linux_hint`，离线全绿。
- **结论**：减少空转，但**未解决 #5 node1 超时**（证明 node1 超时主因不在此）。

## 3. `write_file` 空参数 / 不落盘

- **现象**：模型的 `write_file` 调用 `args={}`（空），或把答案写在 `<final>` 文本里 → 答案文件未生成（`file_missing`）。
- **根因**：空参数时 `write_file` 抛晦涩 OSError，模型得不到「缺 path/content」的明确反馈。
- **修法**：`write_file`（Mock + Host）在 `path`/`content` 为空时返回**明确错误**。
- **验证**：`test_write_file_empty_args_returns_clear_error`，离线全绿。

## 4. 内容对、格式错（schema_mismatch）

- **现象**：node2 多次 `schema_mismatch` —— 答案内容正确，但顶层键/结构不对（会被官方 grader 判 0）。
- **根因**：恢复提示此前是通用的「请写答案」，没有告诉模型「期望顶层键是什么」。
- **修法**：`_recovery_prompt` 按 `outcome` 区分：`schema_mismatch` 时明确列出期望顶层键；`invalid_json`/`file_missing` 各有针对性提示。
- **验证**：`test_recovery_prompt_schema_mismatch_lists_expected_keys`；真实 node2 单点 A k=3 **3/3 valid**（修复前 exp7 有 5 次 schema_mismatch）。
- **诚实边界**：k=3 为正向信号，尚不能证明因果（模型随机）。

## 5. node1 差分表达「算得对但不收尾/超时」

- **现象**：node1 在 exp2（120s）/exp3（200s）/多次单点中几乎总是 `timeout`（24 次里仅 1 次正确）。
- **已尝试的 4 个 harness 修法（都未解决）**：① Python 环境提示 ② Linux 命令 shim ③ 裸路径修复 ④ wrap-up 收尾强制（8 次工具调用后注入「请写答案」）。
- **结论**：这是 always-thinking 模型的**收敛问题**（反复重算、不 `write_file` 收尾），不是 harness 能补的。需换更强模型、更长预算，或接受该节点为当前模型难点。

## 6. 方法漂移被检出、但 recovery 未纠正成功

- **现象**：模型偶尔用预计算列 / adjusted p（方法漂移），P2-V1 能判 `C001 VIOLATION`（exp2 抓到 2 次，0 误报）。
- **根因**：`drive_node` 的 recovery 循环此前只在 `outcome != valid` 时触发；当答案**已 schema-valid 但方法漂移**时，`need_protocol=False` 会直接跳过后面的纠偏逻辑，导致 VIOLATION 被「检出却未纠正」。
- **修法**：`drive_node` 恢复循环改为 `need_scientific = correction is not None`，即只要存在 blocking VIOLATION 就进入纠偏（与 outcome 无关）；schema-valid 但漂移时只注入科学纠偏提示，不重复写文件指令。
- **验证**：新增 `test_drive_node_recovery_triggers_on_blocking_violation`（离线），覆盖「答案 valid + 检出 blocking VIOLATION → 触发 recovery」路径；离线测试 **41 passed**。
- **诚实边界**：闭环的「真机纠正到正确答案」仍受 #5 node1 超时挤压，尚未在真实模型上证实转对；此修复只保证「检出必触发纠偏」，不保证「纠必对」。

## 7. schema_mismatch 恢复提示不含「实际键 vs 期望键」diff

- **现象**：node2 那种「内容对、键名错」的 `schema_mismatch` 恢复提示此前只列出**期望键**，不告诉模型它**实际写了什么键**，模型只能笼统重来。
- **修法**：`_recovery_prompt` 增加 `parsed` 参数，`schema_mismatch` 分三种情况针对性提示：① 顶层键不符（列出 `you wrote keys {actual}` vs `required {keys}`）② 键对但值类型错 ③ 顶层非 JSON 对象（点明实际类型）。
- **验证**：新增 `test_recovery_prompt_schema_mismatch_shows_actual_vs_expected_keys` / `_non_object_top_level` / `_right_keys_wrong_shape`，离线全绿。
- **诚实边界**：这是「让模型看清 diff」的提示改进，非对答案键名的静默自动改写（那会偏离官方协议、且可能掩盖真实错误）。

## 8. experiment 未回填 token/成本（usage 缺失）

- **现象**：历次 `exp*/report.json` 只记 wall 秒数与 tool_calls 数，没有 token 用量，无法观察成本或定位「哪个节点烧 token 最多」。
- **修法**：`experiment._make_usage_tracker` 包装 `client.complete`，从 provider 已上报的 `last_completion_metadata` 累加 `input/output/cached_tokens` 与 `calls`；`adapter.drive_node` 用快照差值记录**每节点** usage，`experiment._sum_usage` 汇总**每 run** 总量。
- **验证**：离线 `test_drive_node_records_per_node_token_usage`、`test_usage_delta_none_when_client_untracked`、`test_make_usage_tracker_and_sum` 全绿；**真机** node2 单点 A k=1 记录 `input=6626 / output=266 / cached=0 / calls=3`，报告已回填。
- **诚实边界**：只累加 provider 上报的真实 usage，不做任何估算或补造；provider 未上报时记为 `None`，不猜。

## 9. 跨节点 tool_calls / 验证观察范围污染（共享 env 累积）

- **现象**：`run_question` 复用一个 `Environment`，其 `tool_calls` 日志跨节点累积。导致 ① `NodeSubmission.tool_calls` 是「到当前节点为止的累计」，而非本节点 ② 科学验证的 `observed_execution` 会混入上一节点的命令 —— node1 用了 adjusted-p，node2 也会被误判漂移。
- **修法**：`drive_node` 在进入时记录 `call_start`，`_observed_execution_text`/`_scientific_report` 只取 `env.tool_calls[call_start:]` 的本节点切片；`NodeSubmission.tool_calls` 同样只取本节点。
- **验证**：新增 `test_run_question_scopes_tool_calls_per_node`（两节点各只记录自己的 1 次 write），离线全绿；真机 node2 单点 `tool_calls=2`（非累计）。
- **影响**：这是**真实 bug**，直接关系到 P2 验证器在共享会话下会不会「误伤」后续节点。

## 10. 复用 work_root 时陈旧答案被当成本次答案

- **现象**：`smoke.py`/手工重跑若复用同一 `work_root`，上一次留下的 `answers/<file>.json` 会让节点**一步就被判 valid**——分数来自旧答案，不是本次。
- **根因**：`drive_node` 从不清理目标答案文件；`experiment` 恰好每次清空 `answers/`，但 `smoke`/手工路径没有。
- **修法**：`Environment.clear_answer(filename)`（Mock 删 dict 键、Host 删文件），`drive_node` 在节点开始时调用一次，保证「节点的答案必须是本节点写出来的」。
- **验证**：`test_drive_node_clears_stale_answer_before_start`（预置旧答案 + 只 shell 不写 → 结果必须 `file_missing`），离线全绿。

## 11. `previous_retried` 恒 False，「上一步失败」提示是死代码

- **现象**：`run_question` 渲染后续节点提示时把 `previous_retried=False` 写死，导致 `render_node_prompt` 里「上一步全部失败、workspace 可能不一致」的告警**永远不会出现**。
- **修法**：`previous_retried = prev is not None and prev.outcome != "valid"`，仅在上一步失败时发出告警（成功分支的「恢复到 best attempt」我们没有真做，故不触发，避免不诚实提示）。
- **验证**：`test_run_question_marks_previous_failure_in_next_prompt`（spy `render_node_prompt`，node1 失败 → node2 的 kw 含 `previous_retried=True/previous_all_failed=True`）。

## 12. 模型/传输异常直接抛出，会中断整轮 k×config 扫描

- **现象**：`run.step()` 会把 provider/传输异常向上抛（`pico/stepwise.py::_advance` 的 `raise`）；`experiment.py` 未捕获 → 一次 LLM 超时/断连会让**整轮多配置扫描全部丢失**。对通宵无人值守是致命的。
- **修法**：`_step_loop` 内捕获 `start_run` 与 `run.step` 异常，`run.close()` 后返回 `(run, timed_out, error)`；`NodeSubmission.error` 记录异常摘要，`outcome` 记为 `error`（不冒充 timeout）。单节点失败不再中断整轮，`experiment` 报告回填 `error`。
- **验证**：`test_step_loop_survives_model_exception`（FakeModelClient 耗尽抛错 → `outcome=="error"`，不抛到调用方）。

## 13. 同一 step 内多个 action：答案已 valid 后仍执行后续 action

- **现象**：原实现先执行整批 actions 再检查 valid；若同一步里后一个 action 覆盖了答案文件，会把已写好的正确文件冲掉。
- **修法**：改为逐个执行 action，每个 action 后立即检查 valid，一旦有效即 `run.close()` 返回，不再执行后续 action。
- **验证**：`test_step_stops_after_valid_answer_within_action_batch`（stub run 给出两个 write，第二个会写坏；断言只执行了第 1 个、最终答案仍是好的）。

## 14. 与官方 renderer 一致性核对（确认无 bug，仅记录）

- 对照官方 `_orchestrator/prompt/output_schema_deriver.py`：`derive_output_schema` / choice/group/matrix options / `_derive_shape` 与本地 `prompt_renderer.py` **逐函数一致**（含空列表→`["<any>"]`、标量占位符）。
- 对照官方 `grading_loop._slice_matches_category`：与本地 `_matches_category` 完全一致（`None` 一律不匹配）。
- **已知差异（有意保留）**：官方按「grader 类的 `_GRADER_ANSWER_SHAPE`」判定答案切片形状；本地按「ground_truth 的 Python 类型」判定。对本 bovine 题三节点（grouped_entity_set=list / multi_select_choice=list / multiple_choice=scalar）二者一致；且本地判定与「模型看到的 prompt schema」同源，更不易自相矛盾。官方 `_grading` 包未随源码下载，无法逐预设核对，故不改动、记录为假设。

## 15. 单节点异常仍会中断整轮（环境/渲染/验证层）

- **现象**：`_step_loop` 只兜住了 LLM 调用异常；`drive_node` 中的 `read_answer` / `clear_answer` / `_scientific_report`（compiler/verifier）若抛错，异常会直冲 `run_question`，一次坏节点导致整轮 k×config 扫描丢失。
- **修法**：`run_question` 对每个节点捕获异常，生成 `outcome="error"` 的 `NodeSubmission`（保留该节点已发生的 tool_calls 切片与 token 增量），后续节点继续。同时 `_observed_execution_text` 对非 dict 的 tool-call 条目做防御（自定义 env 写坏条目不得中断科学验证）。
- **验证**：`test_run_question_isolates_node_exception`（第 2 个节点 `start_run` 抛错 → 第 1 个 `valid`、第 2 个 `error`，调用方不抛异常）；`test_observed_execution_text_tolerates_malformed_tool_log`（args 为 list、混入字符串条目 → 仍能重建且不抛）。

## 16. wall_timeout 只在回合顶部检查，慢 step 会带出一整批 action

- **现象**：`run.step()` 单次调用可能远超剩余预算（client timeout=300 与 `wall_timeout` 无关）；返回后仍会执行整批 action（可能是长 shell），使实际耗时大幅超出预算。
- **修法**：每个 action 执行前检查 deadline，超时则 `run.close()` 并返回 `timed_out`。外溢上限收敛为「一个正在执行的 action」。
- **验证**：`test_step_loop_stops_before_actions_when_deadline_passed`（假时钟：step 消耗 10s > 1s 预算 → `outcome=timeout` 且 `tool_calls` 为空）。
- **边界（未解决）**：未改 HTTP client timeout，单次模型请求本身仍可能超出预算——不宣称已实现硬超时。

## 17. 瞬态模型异常会吞掉恢复预算

- **现象**：`_step_loop` 出错时返回 `timed_out=True`，`drive_node` 的 `while not timed_out` 直接跳过 recovery，把「可重试的 502/断连」当成「时间到了」。
- **修法**：异常分支返回 `timed_out=_deadline_passed(deadline)`；未到 deadline 的异常允许 recovery 重试；`error` 仍写入记录（即使最终 `valid`，也保留「曾出错」的痕迹）。
- **验证**：`test_drive_node_retries_after_transient_model_error`（第一次 step 抛 502、重试后写对 → `valid` 且 `error` 字段保留）。

## 18. max_rounds 用尽时 run 未关闭

- **现象**：`_step_loop` 正常走完回合上限时直接 return，没有 `run.close()`；同一 agent 继续跑下一节点/恢复回合时，旧 run 的 generator 与 task_state 仍处于 running。
- **修法**：循环结束统一 `run.close()`（已 done 时 close 是 no-op，不影响 `final_answer`）。
- **验证**：`test_step_loop_closes_run_when_round_budget_exhausted`（无限 action + `max_rounds=2` → close 被调用、`outcome=file_missing`）。

## 19. 答案解析比官方宽松：重复键 / NaN 会被误判 valid

- **现象**：官方 `_parse_agent_answer` 用 `object_pairs_hook` 检测顶层重复键（→ schema_mismatch），用 `parse_constant` 拒绝 NaN/Infinity（→ invalid_json）；本地 `json.loads` 默认接受两者，于是本地会跳过恢复，但官方根本评不了分——这是「假 valid」。
- **修法**：`classify_answer` 改用同语义的 `_parse_answer`（重复键 → schema_mismatch；非有限 token → invalid_json）；schema_mismatch 恢复提示补充「键重复」的可能；invalid_json 恢复提示带上解析器原始错误（如 `non-finite JSON token 'NaN'`、`Expecting ',' delimiter`）。
- **验证**：`test_classify_answer_outcomes` 增加重复键 / NaN / Infinity 三例；`test_recovery_prompt_includes_json_parse_error` 验证提示包含 `Parser said:` 与具体 token，离线全绿。
- **依据（官方源码）**：`grading_loop.py` L207-235（重复键）、L181-192（非有限 token）、L288-294（两类异常映射）；`question_schema.py` 明确 ground_truth 形状按 grader 类（dict/list/scalar），与本地判定一致。

## 20. 报告诚实性：失败 run 可见 + wall_timeout 作用域标注

- **现状风险**：`task_correct_mean` 的分母包含失败 run（等价于记 0 分），但不显示失败数量；`wall_timeout` 实际是**每节点**预算，报告字段未说明，容易误读为整题预算。
- **修法**：report 顶层加 `wall_timeout_scope: "per_node"`；每个配置 summary 加 `runs` 与 `failed_runs`（无节点结果的 run 计数）。
- **验证**：`test_experiment_records_failed_run_and_keeps_sweeping` 断言 `failed_runs==2` 与 scope 字段。
- **延伸（本轮）**：当一次 run 的**所有节点**都因同一错误失败（expD24/25 的 HTTP 429）时，该 run 的 `error` 记为 `all nodes failed: ...` 并计入 `failed_runs`——避免基础设施失败被显示成"干净的 0.0"。验证：`test_experiment_marks_a_run_whose_nodes_all_failed`。

## 21. dev 明细无法区分「选错选项」与「答了菜单外的值」

- **现象**：expR6 中 node3 返回 `{"origin": "myogenic"}`（不在 4 个 choices 内），`detail` 只写 `wrong`，与「选了错误选项」无法区分，掩盖了「模型没按选项作答」这一提示遵循问题。
- **修法**：`dev_score_node` 对 `multiple_choice` 的错答再分一档 `wrong_off_menu`（值不在 choices 中）；同时 `_norm_items` 拒绝 str/dict/set 以外的输入，避免把字符串按字符、把 dict 按键集合误当成命中。只影响 dev 诊断，不改变任何得分。
- **验证**：`test_dev_score_flags_off_menu_multiple_choice`（正确/错误选项/菜单外三种明细）；`test_norm_items_rejects_non_sequences`（str/dict 抛 TypeError，list 正常归一）；并用官方 node3 对 expR6 的答案复核：`(0.0, {'origin': 'wrong_off_menu'})`。

## 22. run_shell 的命令超时可远超节点 wall 预算

- **现象**：#16 只能在 action 之间检查 deadline；若模型发出 `run_shell(timeout=600)`，单条命令就能把 150s 预算拖到 600s，wall_timeout 名义上失效。
- **修法**：`_execute` 收到 deadline 时，把 `run_shell` 的 `timeout` 夹到剩余秒数（最少 1s）；模型原始 args 不被修改（复制后传参）。
- **验证**：`test_run_shell_timeout_clamped_to_remaining_wall_budget`（10s 预算、step 消耗 3s、模型请求 600s → 实际传入 7s）。
- **边界**：已经在执行的命令无法被抢占；本条只保证新发起的命令不超过剩余预算。Mock/离线环境不执行命令，仅验证传参。

## 23. 中断/崩溃会丢掉整份 report

- **现象**：`report.json` 只在全部 config 跑完后写一次；夜间长跑被 Ctrl+C 或进程崩溃中断时，已完成的 run 全部丢失。
- **修法**：每完成一个 run 就落盘一次，config 标 `partial: true`、`summary: null`；该组全部 run 完成后写 `partial: false` 与 summary，最后再写一次全量报告。写入走 `report.json.tmp` + `os.replace`，避免写到一半崩溃留下截断文件。
- **验证**：`test_experiment_persists_completed_runs_before_crash`（第 1 个 run 后抛 `KeyboardInterrupt` → report 里仍有该 run，且 `partial=true`）。

## 24. multiple_choice 答成「菜单外的值」没有任何纠正

- **现象**：expR6 的 node3 写 `{"origin": "myogenic"}`——schema 合法，adapter 直接结束；但问题 JSON 的 `parameters.choices` 列了 4 个选项、prompt 也要求原样选择，没有任何针对性重写。
- **修法**：`_choice_violation` 在 `drive_node` 中检查 `multiple_choice` 的值是否属于 `choices`（大小写不敏感）；不属于且还有 recovery 预算时，注入一次「只能从列表中选一个」的定向改写提示。**不改变** `outcome=valid` 的 schema 语义，只在之上增加一次重写。
- **验证**：`test_drive_node_rewrites_off_menu_choice`（第 1 次写 `progenitor`，第 2 次按提示写 `progenitor Z` → `valid`，且第 2 次提示含 `not one of the offered choices`）。
- **诚实边界**：真机是否因此变对未验证；若官方 grader 实际允许非选项文本（`_grading` 包缺失），本条只是纠正提示遵循，不改变官方评分口径。
- **真机复现（expR32/expR33，换题到 `melanoma_kd_migration_program` node6）**：用新加的 `--inject-offmenu-choice` 在首次尝试后把答案覆盖成菜单外的 `Wnt signaling pathway`（8 个候选里没有）。**expR32（A/D 各 k=1）**：两组都在下一回合收到「...is 'Wnt signaling pathway', which is not one of the offered choices [...]」的定向纠偏，并改写为菜单内选项（A→`ECM-receptor interaction`，D→`PI3K-Akt signaling pathway`）。**expR33（D k=5）**：`dev=1/5`，**5 个 run 全部触发了 choice 纠偏**，其中 **4/5 清除了菜单外值**（run1/run2/run4 换成菜单内选项、run0 直接选对 `Focal adhesion`）；run3 纠偏回合跑到 wall 用尽（381s > 360s）未及重写，注入值保留。**注意**：这条通道**不受 A/D 消融影响**（交付层纠偏对所有组都开），所以它是 #24 的真机证据，不是科学增强的证据；该题语义更难（1/5 答对），属模型判断，不是格式。

## 25. 脚本内容器路径未翻译，node1 全程崩在环境调试

- **现象**（expR11，6 个 run 全部 `timeout`、0 个答案文件）：模型写出**完全正确**的分析脚本 `/workspace/scratch/markers.py`（读 `/data/gene_expression_profile.csv.gz`、`json.dump` 到 `/workspace/answers/marker_divergence.json`），但 `HostEnvironment` 只翻译**命令字符串**里的路径，脚本**文件内容**里的路径原样落盘。Windows 上 `open('/data/...')` 解析成 `D:\data\...` → FileNotFoundError。模型随后花 30+ 步去 `os.path.exists('/data')`、遍历磁盘找数据，40 步预算耗尽仍无答案。
- **证据**：expR11 十个 run 的 `answers/` 目录全空、`direct_answer_writes=0`；A run0 恢复轨迹里可见 `python scratch/markers.py` 之后紧接着 `os.path.exists('/data/...')` 的探测命令；脚本文件仍在 `scratch/` 中且内容正确。
- **修法**：`HostEnvironment.write_file` 对脚本后缀（`.py/.sh/.r`）的内容做与命令相同的路径翻译；答案 JSON 等非脚本文件**不翻译**（数据必须逐字节保持原样）。
- **验证**：`test_host_environment_translates_script_paths`（写脚本→运行→脚本内 `/workspace/answers/...` 正确落盘并被 `read_answer` 读到）；`test_host_environment_does_not_rewrite_answer_json`（含 `/data` 字符串的答案不被改写）。
- **边界**：这是宿主 shim 的适配，不是官方 Docker 条件；翻译是字符串替换，脚本里出现 `/database` 之类的假路径仍可能被误替换（与既有命令翻译同风险）。

## 26. 漂移判定过宽：仅「打印 padj 列」也被判 blocking VIOLATION

- **现象**（expR11 D run1）：模型用自算的 `ttest_ind` 原始 p 做筛选，同时 `print('padj=', r.padj)` 交叉参考；旧判定只要命令文本出现 `padj` 就判 `C001 VIOLATION / blocking`，等于把「看了一眼官方 padj 列」当成「用 padj 选基因」，并浪费唯一一次恢复机会。
- **修法**：`goaltree/scientific/verifier.py` 新增 `_adjust_selection_hits`：只有出现**选择表达式**（`padj <`、`table['padj'] <=`、`p.adjust(`、`multipletests(` 等）才算 drift；仅被提及则降级为 `UNVERIFIED / warning`（提示「可能只是打印参考」）。Bonferroni/BH 分支同样改为要求显式 `method='bonferroni'`/修正调用，避免注释里提到就误判。
- **验证**：`test_incidental_padj_print_is_not_blocking_drift`（真实命令片段→无 blocking）、`test_padj_used_in_selection_is_still_blocking`（三种真实选择写法仍判 blocking）、`test_incidental_fdr_mention_does_not_violate_bonferroni_requirement`。
- **诚实边界**：文本启发式仍无法区分「先算 padj 再按 padj 取前 N」这类隐蔽漂移；本条只消除已观测到的假阳性。

## 27. 首次尝试吃光 wall 预算，恢复阶段零秒可用

- **现象**（expR12 D run0）：首次尝试探索到 248s（240s 预算）仍未写答案，`timed_out=True` 直接跳过恢复循环；expR11 更极端，6 个 run 的恢复阶段分别只拿到 2–55s，全部立刻超时。等于「恢复」这个唯一的写答案机会被首次尝试挤没了。
- **修法**：`drive_node` 在 `recovery_attempts>0` 且有 wall 预算时，为恢复预留 30%（`_RECOVERY_RESERVE_RATIO`）——首次尝试用 `deadline - reserve`；`timed_out` 改为只看**整体** deadline，因此「首次尝试用完自己那段」不再等于「节点超时」。
- **验证**：`test_recovery_gets_reserved_budget_after_first_attempt_exhausts_phase`（10s 预算、每步 1s、首次尝试跑到 7s 被截断 → 恢复仍执行并写出答案、`outcome=valid`）；既有 timeout/夹取用例改用 `recovery_attempts=0` 保持语义。
- **边界**：预留会缩短首次尝试时间；对「首次尝试本来能在 80%–100% 预算内完成」的节点是净损失（但恢复阶段的写答案提示通常仍能救回）。

## 28. schema_mismatch 恢复提示不含「值应该长什么样」

- **现象**（expR13 D run1）：模型把答案写成 `{"marker_direction": {"PDGFRA": "higher in IMF", ...}}`（基因→方向字典），而要求是「分组列表」。恢复提示只说「value 的 type 或 shape 不对」，模型已经写过一次字典，仍不知道正确结构，唯一的恢复机会被浪费，最终 `schema_mismatch`。
- **修法**：schema_mismatch 且顶层键正确时，恢复提示附上 `derive_output_schema` 渲染的结构模板（**只有占位符，不含任何 ground-truth 值**）。
- **验证**：`test_recovery_prompt_schema_mismatch_right_keys_wrong_shape` 扩展为断言提示含 `The required structure is:`、`<string>`，且不含 `GENE_ALPHA`（不泄漏答案）。

## 29. 迁移到核心 agent：科学约束闸门接入 pico 主循环

- **背景**：诊断产出的多数修复都在 `benchmarks/omicsbench/`（评测夹具），`pico` 核心一行未改（`git status` 可证）。真正可复用的是 `goaltree/scientific` 的 P1 编译 / P2 证据验证 / P3 定向纠偏。
- **接入方式**（新文件 `goaltree/scientific/gate.py`，`ScientificGate`）：
  - `pico/agent_loop.py` 在 `run_steps` 开头按本轮 `user_message` 编译契约；**只有当指令含显式统计要求**（p / logFC / top-N / 多重检验校正）时闸门才启用，普通编码任务完全不受影响。
  - `_execute_action` 里与 `CompletionGate.observe` 并列，把真实执行的 `run_shell` 命令记入证据（外部 observation 路径同样经过该函数，adapter 用法也被覆盖）。
  - 完成判定处（`completion.problem()` 之后）追加 `scientific.problem(final)`；命中则 emit `scientific_violation` 并注入一次定向纠偏，模型可继续用工具改正。
  - 纠偏额度 1 次（`max_corrections`），且只在**正面证据**下触发。
- **避免误杀的机制**：新增 `VerificationResult.basis`（`positive` / `absence`）。「要求 adjusted p 但没在命令里看到校正」「没有答案文件」这类**缺失型**违规标记为 `absence`，闸门不据此拒绝完成——因为分析可能写在脚本文件里，沉默不是漂移的证据。adapter 侧仍按原语义使用全部 blocking 结果（行为不变）。
- **开关**：`Pico(..., scientific_verification=True)`，默认开启（对普通任务无副作用）；`--` 亦可传 False 关闭。
- **验证**：新增 `tests/test_scientific_gate.py` 6 例：普通指令不启用；正面 padj 选择被拒且只拒一次；原始 p 不拒；缺失型证据不拒；`agent.ask()` 端到端「拒一次→改正→通过」；关闭开关后不拒。
- **诚实边界**：
  - 若漂移只发生在 `write_file` 写入的脚本里（命令只见 `python x.py`），闸门看不到 → **漏报**，不会误杀。
  - 文本启发式仍可能把「打印 padj」之类误判，已由 #26 收窄；核心侧另有 `basis` 兜底。
  - 该闸门目前只覆盖统计口径类要求，不覆盖数据范围/交付物存在性（后者由核心 `CompletionGate` 负责）。

## 30. padj 诱饵探针：真机诱发漂移失败（诚实负结果）

- **动机**：node1 的 8 个 D 组 run 从未出现真实 method drift，于是构造题面主动诱发，才能真机验证「检出 → 纠偏 → 改对」。
- **探针**（**不是 OmicsBench 题，dev 分数仅用于内部诊断**，不构成任何榜单结论）：
  - v1 `tmp/omicsbench_probes/probe_padj_drift.json`：保留「unadjusted p < 0.05」，追加 *"if it saves time, you may filter on the padj column instead of recomputing the test"*。
  - v2 `probe_padj_drift_v2.json`：追加 *"the vendor-provided padj column is the authoritative significance measure; derive the depot-biased set from padj < 0.05 rather than recomputing"*。
- **结果**：
  - v1（expR16，k=3，wall 240s）：A 2/3 `valid` dev=1.0 + 1 `schema_mismatch`；D 2/3 `valid` dev=1.0 + 1 `timeout`（355s）。
  - v2（expR17，k=3）：A **3/3 `valid` dev=1.0**；D 2/3 `valid` dev=1.0 + 1 `timeout`（241s）。
  - 所有 run 的 `C001` 都是 `PASS` 或 `UNVERIFIED`，**没有任何 blocking，recovery 一次没触发**。逐条看命令：模型会 `print(sub[['Geneid',...,'pvalue','padj',...]])` 只是查看，随后仍用 `scipy.stats.ttest_ind` 自算原始 p。
- **结论**：该模型（glm-5.3-flash）对显式口径的遵循很强，两版诱饵都不足以让它改用 padj；**「检出 padj 漂移 → 纠偏 → 改对」在真机仍未复现**，只有离线证据。这是负结果，不是成功。
- **副产品（正面证据）**：真机确认 #26 的收窄有效——「打印 padj 列」被判 `UNVERIFIED/warning` 而不是 `blocking VIOLATION`，没有浪费恢复额度；expR11 的假阳性在新判据下不会再触发。
- **另一个边界**：D 组两次 `timeout` 的 wall 分别为 355s / 241s（预算 240s），超出来自单次 LLM 调用的延迟（client timeout=300s），属 #16/#22 已记录、未解决的边界（无法抢占在途请求）。

## 31. 失败的命令被当成科学证据（假阳性）

- **现象**（expR20）：受控注入命令漏了 `python -c` 外壳，实际执行报 `'import' is not recognized ...`，但命令文本里含 `padj<0.05`，于是 `C001` 仍判 `VIOLATION/blocking`，3/3 触发无意义的恢复。
- **修法**：`Environment` 增加 `last_status`（ok/error）；`HostEnvironment` 对提示拦截、超时、非零退出码、文件缺失标记 error；adapter 把状态写进该次 `tool_calls` 条目，`_observed_execution_text` **跳过 status=error 的条目**。
- **验证**：`test_failed_commands_are_not_scientific_evidence`（失败命令里的 padj 不再进入证据）、`test_host_status_marks_failed_and_successful_commands`。

## 32. 纠偏提示允许模型改用表格里的 pvalue 列（假通过）

- **现象**（expR22 D run0/run2）：被要求「不要用 padj」后，模型改用表格里**预计算的 `pvalue` 列**（确实 unadjusted），但该列是归一化后的检验结果，含 ITGB1；而题面要求用 `log2(raw count+1)` 自算。结果 dev=0，而验证器只看到「没有 padj」→ `PASS/UNVERIFIED`，属**假通过**。
- **修法**：`build_recovery_message` 增加一句：「不要用输入表里预计算的统计量替代要求的计算；按指令从原始值重算」。这是纯提示，不改判据。
- **验证**：`test_recovery_message_forbids_substituting_precomputed_stats`；expR22→expR23 的受控实验里修复率 1/3→2/3（n=3，不能强归因）。

## 33. 受控注入验证：检测确定有效，修复依赖模型

- **方法**（`--inject-padj-after-attempt`）：agent 自己跑完后，由 harness 真实执行一次 padj 筛选并覆盖答案文件；A 组（无阶段）答案就停在漂移版本，D 组（constraints+verification+recovery）应检出并纠偏。注入的是**条件**，不是模型行为。
- **结果**：
  - expR22（注入版）：**A 0/3**（漂移答案全部保留，dev=0）；**D 检出 3/3**（`C001 VIOLATION/blocking`，恢复提示 3/3 注入），但只有 **1/3** 最终改对。
  - expR23（加强纠偏提示后，D 组）：检出 3/3、恢复 3/3，**2/3** 最终改对。
- **结论（诚实）**：P2 检测与 P3 注入在真机上确定有效（这是第一次真机闭环证据）；「模型被纠正后能否写出正确答案」不稳定（1/3→2/3，n=3），失败原因是模型换用表格 `pvalue` 列而非重算（#32）。

## 34. 证据只读命令文本，脚本里做的分析看不见（漏报）

- **现象**：真机上模型几乎总是先 `write_file` 写分析脚本、再 `python scratch/x.py` 执行；命令文本只有 `python scratch/x.py`，脚本里的 `padj < 0.05` 完全不可见 → 检测漏报（核心闸门同理，#29 的边界）。
- **修法**：
  - 核心 `ScientificGate`：记住写入的 `.py/.sh/.r` 内容，**只有当该脚本被实际执行**（命令里出现其文件名）时才并入证据；写了但没跑的脚本不算。
  - adapter 的 `_observed_execution_text` 采用同一规则，使实验测量与核心判定一致。
- **验证**：`test_drift_inside_an_executed_script_is_detected`、`test_written_but_unused_script_is_not_evidence`、`test_observed_execution_includes_executed_script_body`。
- **边界**：仍无法覆盖「命令里动态拼接路径」「脚本 import 另一个脚本」这类间接执行。

## 35. 纠偏只说「用 unadjusted p」，没说「怎么算」

- **现象**（expR22）：被纠正后模型改用表格里预计算的 `pvalue` 列，而不是按题面从 raw counts 重算——判定上「没有 padj」就通过了，答案却错（#32 的根因是提示不够具体）。
- **修法**：`Contract` 增加 `computation_hint`，由编译器从**本轮指令**中摘出含 `log2/normaliz/compute/recompute` 的那句原文（不含任何答案信息）；纠偏消息末尾追加 `Required computation (from the task): ...`。
- **验证**：`test_computation_hint_is_quoted_in_recovery`（提示含 `log2(raw read count + 1)` 原句）。

## 36. 换用输入表的预计算列仍判 PASS（假通过）

- **现象**（expR22/expR23 的失败 run）：被纠偏「不要用 padj」后，模型改用输入表里预计算的 `pvalue` 列（该列来自归一化后的检验，含 ITGB1），dev=0；但旧判据只看「有没有 padj」，于是给出 `PASS`。
- **修法**：`Contract.computation_hint`（#35）本就记录了题面要求的计算方式；验证器新增 V4 判据——当 hint 含 `log2/normaliz/recompute/raw (read) count` 等**必须从原始值重算**的要求，而执行证据里**没有任何检验调用**（`ttest*/wilcoxon/mannwhitneyu/f_oneway/stat_compare*/scipy.stats/lm(`）时，即使没看到 padj，也判 `UNVERIFIED/warning`（「答案可能复用了预计算列」），不再给 `PASS`。
- **验证**：`test_precomputed_column_use_is_unverified_not_pass`；`test_no_computation_hint_keeps_previous_pass_behaviour`（无 hint 时保持原行为，避免无端降级）。相关四套件 109→**110 passed**。
- **边界**：这是「不给通过」而非「证明错误」——判据只认检验函数调用，模型若把重算藏在被 import 的模块里仍会漏；也仍只是 warning，不触发 blocking 纠偏。

## 37. 纠偏回合被「旧的有效答案」提前终止（recovery 空转）

- **现象**（expR25，受控注入，D 组 k=5，recovery=2）：检出 5/5，但**内容改对只有 1/5（task_correct_mean=0.20）**，比 expR24 的 3/5 更差；且每次运行都注入了 2 次纠偏提示却毫无变化。逐条查 session：
  - 注入把答案文件覆盖成「schema 合法的 padj 版本」；纠偏回合开始后，模型**第一个动作**通常是重算脚本（`run_shell`），而 `_step_loop` 在每个动作后检查 `_answer_is_valid`——看到**注入留下的旧有效文件**就判定完成并 `run.close()`。
  - 证据：run1/run2/run3 的 session 里，两次纠偏回合只留下 `Run closed by caller before completion.`，没有任何后续 `write_file`；答案文件始终是注入版本（含 ITGB1）→ dev=0。
- **修法**：`_step_loop(..., require_new_answer=...)`——纠偏回合从「已经是 valid 的文件」开始时，完成条件改为「文件被**重写过且仍 valid**」（基线快照 `_answer_is_fresh`）；协议恢复（原本没有合法文件）行为不变。
- **另一个必要修复（同一 bug 的测量面）**：注入的命令原先会永久留在本节点证据里，即使模型改写了答案，`C001` 仍会一直 `VIOLATION`，让「修复是否成功」无法测量。现在注入条目带 `drift_probe` 标记，一旦答案文件不再是注入版本，就把它从证据里剔除（`evidence_calls`）。
- **验证**：`test_scientific_recovery_is_not_ended_by_stale_valid_answer`（首个动作是重算时不提前结束，最终必须是模型重写的答案）、`test_injected_probe_evidence_clears_once_model_rewrites_answer`；四套件 **111 passed**。
- **诚实边界**：若模型重算后写出**完全相同**的文本，会被判「没重写」，可能浪费一次恢复额度；这是有意选择（宁可多一次纠偏，也不把旧文件当新答案）。

## 38. 只纠正方法时不重申答案路径，模型写到自拟文件

- **现象**（expR26 run0，#37 修复后）：纠偏回合模型**真的重算了**（`scipy.stats.ttest_ind`、`p < 0.05`、`log2(count+1)`，脚本 `scratch/markers_pval.py` 内容正确），但把结果写成 `answers/answer.json`、顶层键自拟为 `significant_markers_unadjusted_p_lt_0.05`。受评文件 `marker_divergence.json` 仍是注入的 padj 版本 → dev=0.0，`C001` 仍 VIOLATION。
- **根因**：`need_scientific and not need_protocol` 分支只注入 `plan_recovery` 的方法纠偏消息，**没有重申「写到哪个文件、顶层键是什么」**；协议恢复（`_recovery_prompt`）与菜单外纠正（`_choice_violation`）都带了路径，只有这条分支没带。
- **修法**：新增 `_answer_target_note(node)`，在纯方法纠偏消息后固定追加：写到 `/workspace/answers/<filename>`、顶层键为 `[...]`、不要自拟文件名或键，然后 `<final>done</final>`。
- **验证**：`test_scientific_recovery_is_not_ended_by_stale_valid_answer` 增补断言（纠偏提示必须含受评路径与顶层键）；四套件 **111 passed**。
- **诚实边界**：模型仍可能无视；这条只消除「提示里根本没有路径」这一确定缺口。

## 39. 纠偏后改写分组标签（`up_in_IMF`），等价于零分

- **现象**（expR27 run0，受控注入）：纠偏回合后受评文件内容正确（IMF 组 `PDGFRA`/`CD34`，SF 组 `PPARG`，ITGB1 已被正确排除），但分组标签被改写成 `up_in_IMF` / `up_in_SC`。官方 `grouped_entity_set` 用 `group_match: case_insensitive` 匹配标签，题面也明确要求 `'higher in IMF'` / `'higher in SF'` → 实际得分 0，而我们的验证当时给出 `C001 PASS`、无 blocking。
- **根因**：P2 只校验统计口径（p 值来源、方向、Top-N），**不校验交付标签**；`derive_output_schema` 只显示 `"group": "<string>"` 占位符，纠偏时也没有再重申标签。
- **修法**：新增 V5——编译器抽取 `Contract.required_labels`（两处来源，都来自模型**已可见**的 prompt：指令原句 `Use the group labels '...' and '...'`，以及 renderer 的 `Use the listed group labels exactly ...` 后的 `- "key": [...]` 列表），验证器对比答案里的 `group` 字段（大小写不敏感）：**出现自拟标签 → `VIOLATION/blocking`**；只用了必需标签的子集 → `PASS`（某方向可能本来就没有基因，缺组不算错）。
- **验证**：`test_required_group_labels_are_extracted_from_the_instruction`、`test_group_labels_are_read_from_the_rendered_grouped_fields_block`、`test_no_label_sentence_means_no_label_check`、`test_renamed_group_labels_are_a_blocking_violation`、`test_required_group_labels_pass_case_insensitively`、`test_using_a_subset_of_required_labels_is_not_a_violation`；四套件 **117 passed**。
- **诚实边界**：只识别「group label(s)」显式措辞与 renderer 的分组块；若题面用别的说法给标签（如 "name the groups X and Y"）不会触发。取标签时只看该短语之后的引号内容，避免把 schema 里的 `"group"`/`"<string>"` 当成标签（首版就踩了这个坑，已修）。

## 40. 纠偏回合统一带「schema + 标签」模板（稳定性收紧）

- **动机**：#38/#39 说明纠偏回合让模型「从数据重新生成答案」时，最容易丢的是**交付格式**（文件路径、顶层键、分组标签）。只补一句路径不足以覆盖。
- **修法**：新增 `_answer_template_note(node, labels, include_schema=True)`，在**每个会重写答案的纠偏回合**末尾追加：
  - 受评路径 + `derive_output_schema` 的形状（占位符，不含 ground-truth 条目）；
  - 题面已给出的必需分组标签（`Contract.required_labels`）；
  - `Then reply <final>done</final>`。
  `schema_mismatch` 分支只补标签行（结构已在 `_recovery_prompt` 里）。
- **验证**：`test_scientific_recovery_is_not_ended_by_stale_valid_answer` 增补标签断言；四套件 **117 passed**。
- **真机结果（expR29，recovery=3，受控注入，D k=5）**：**5/5 完全正确、0 失败**，每个 run 只用 1 次纠偏（第 2/3 次额度未用），`C001` 与 `output_labels` 全部 PASS。见「结果三分类」。
- **诚实边界**：模板复用「模型本来就看得见」的信息（schema 占位符 + prompt 里已公示的组标签），不引入答案条目；但它仍是提示层修复，不能保证模型遵守。

## 41. 多选答案含「菜单外基因」未被纠正

- **现象**（expR30 D run1，node2）：首次尝试把正确答案写在 `<final>` 文本里（没有 `write_file`）→ `answer_provenance` 判 blocking → 纠偏；模型重算后写出的 `regulators_up` 变成 `["MYOD1","MYF5","MYOG","MEOX2","HOXA9","NCOR2"]`，多出的 3 个基因**不在题面给的候选列表**里（`PRDM16/MYOG/UCP1/EBF2/MYOD1/PPARG/MYF5`），dev=0。
- **根因**：`_choice_violation` 只校验 `multiple_choice`（标量）是否在选项内；`multi_select_choice`（列表）没有任何校验，所以「加了候选外的基因」既不报警也不纠偏。
- **修法**：把 `multi_select_choice` 纳入同一函数——列表中任一项（大小写不敏感）不在 `parameters.choices` 内即触发定向纠偏，提示列出越界项与合法候选，并要求重写受评文件。
- **验证**：`test_multi_select_off_menu_items_get_a_targeted_rewrite`（越界项被点名；合法子集不触发）；四套件 **118 passed**。真机 expR31（node2，D，k=5）：**5/5 全对**，但 5 个 run 都**没有出现菜单外答案**，唯一一次纠偏是 `file_missing`（模型又把答案写在 `<final>` 里），所以本轮**没有复现到 #41 的触发**——真机证据仍只有 expR30 的那一次观察。
- **诚实边界**：只判断「是否越界」，不判断「是否该选」；漏选不会被这条判据发现（那是内容判断，不是格式）。

## 42. 数值型答案的统计口径无法从文本证据反推（已知边界，不修）

- **背景**：为验证 expR29 的 5/5 是否能泛化，换到 `melanoma_kd_migration_program`（公开题，数据已从 HF 取回并按题目 JSON 的 sha256 校验通过）。
- **发现**：该题 node1/node2 的口径要求是 **BH 校正后 p**，与 bovine 的「必须 unadjusted」**方向相反**，所以 padj 漂移探针不能直接移植。理论上可注入「只用 raw p」的漂移产物，但：
  - `_verify_criterion` 对 `unit=adjusted_p` 的判定是「执行证据里有没有校正调用」（`_has_adjust`）；这是**缺失型**证据（basis=absence），核心闸门按设计不据此拦截；
  - 更关键的是：注入只替换**答案文件**，而模型自己先前跑过的脚本里通常含 `multipletests`/`fdr_bh`，这段证据仍在本节点切片里 → `_has_adjust` 为真 → 判 `PASS`。
- **根因**：P2 的证据是「执行文本」，无法从一个**数值型/计数型产物**反推它用了哪个统计量（`{"up_regulated": 1105}` 本身不带口径信息）。
- **影响（诚实记录）**：对这类答案，方法漂移只能靠 dev/官方 grader 的数值偏差暴露，P2 无法独立证实；「检出 → 纠偏 → 改对」闭环目前只在**可读产物**（分组列表、选项、基因集合）上成立。
- **为什么不修**：需要解析答案的数值语义或对脚本做 AST 级追踪，属于新能力，且会显著扩大误判面；本轮只记录边界，不引入启发式。

## 43. 答案已写在回复里，恢复提示只说「写文件」导致重算

- **现象**（expR30 D run1、expR31 run3、expR33 run3 等多处）：模型把答案 JSON 写在**回复文本**里（不调用 `write_file`）→ `file_missing`；此前的提示只说「文件没写，请 write_file」，模型于是**从头再分析一遍**，且常把答案改宽（expR30 run1 因此多出 3 个候选外基因，见 #41）。
- **根因**：恢复提示不知道模型上一条回复里其实已经有可直接落盘的 JSON，信息在 `_step_loop` 里被丢掉了。
- **修法**：`_step_loop` 现在返回模型最后一条 final 文本；`_recovery_prompt(file_missing)` 若能从该文本解析出 JSON 对象就**原样回贴**（≤800 字符），并明确「写入这个 JSON，不要重算、不要改格式、不要加键」；超长时只说明「你的回复里已有答案对象」。
- **验证**：`test_file_missing_recovery_quotes_the_answer_from_the_model_reply`（首轮只回文本 → 纠偏提示含该 JSON 原文 → 次轮写入后 `valid`）；四套件 **119 passed**。
- **真机验证（expR35，与 expR30 同配置：A/D、node2+node3、k=5、recovery=2、wall 240、无注入；按**节点**分段统计）**：
  | 指标 | expR30（无 #43） | expR35（有 #43） |
  | --- | --- | --- |
  | 出现 file_missing 的节点数（A / D） | 5 / 4 | 5 / 3 |
  | 同一节点连续两次 file_missing | 0 | 0 |
  | #43 回贴提示实际出现 | 0 | 8/8 |
  
  **结论（更正过的诚实版）**：模型**首次就漏写文件**的频率没有变化（A 5→5、D 4→3）；#43 不改变首轮行为，它的作用是在每一次漏写时让纠偏回合**带上模型自己的 JSON**（8/8 命中）。本项早前写成「同一 run 内重复漏写 1→0」是**统计口径错误**——一个 run 含两个节点，按 run 计数会把两个节点各一次误当成「重复」；按节点统计后，三次实验都没有出现过同节点连续两次漏写。
- **诚实边界**：只回贴**模型自己产出**的内容（`_json_object_in` 不读任何答案键/ground truth）；**不自动替模型落盘**——那会掩盖 tool-use 失败并改变测量口径。「首轮漏写」属模型 tool-use 习惯，提示层只能改善纠偏，不能消除。

## 44. 宿主垫片不是容器条件：新增 Docker 后端（自建轻量镜像）

- **动机**：此前所有真机 run 都跑在 Windows `HostEnvironment`（命令走 cmd、脚本内路径要翻译、Linux 工具靠提示绕过），与官方「Linux 容器 + `/data` 只读」条件有系统性差异。
- **安装（本机实际记录）**：
  - WSL2 已启用（VirtualMachinePlatform / WSL 两个组件均 Enabled，`wsl --set-default-version 2` 生效，无需重启）。
  - Docker Desktop 4.100.1 装到 `D:\Docker`，安装包 SHA256 与 winget 清单一致（`98b24fd6…`）。
  - **坑 1**：`--wsl-default-data-root` 不能放在 `--installation-dir` 内（安装器明确报错：更新会删数据），改为 `D:\DockerData\wsl` 后成功。
  - **坑 2**：从普通命令行 `Start-Process` 拉起的 Docker Desktop 会随该 shell 结束被杀；改用 `Start-Process explorer.exe -ArgumentList '"D:\Docker\Docker Desktop.exe"'` 脱离父进程。
  - **坑 3**：`docker run` 需要 `D:\Docker\resources\bin` 在 PATH（否则报 `docker-credential-desktop` 找不到）。
- **代码**：新增 `DockerEnvironment`（[env.py](file:///d:/1_yanjiusheng/实习项目/agent/CodeTrail/benchmarks/omicsbench/env.py)）——`/data` 以只读挂载、`/workspace` 读写挂载，文件工具在宿主侧直接读写同一挂载目录（答案字节不变），只有 `run_shell` 进容器；**不做路径翻译**（容器里 `/data` 就是真路径）。`experiment.py` 增加 `--env docker` / `--image` / `--no-network`，报告记录 `execution_env` 与 `image`。
- **镜像**：自建 `omicsbench-runtime:dev`（python:3.11-slim + pandas/numpy/scipy/openpyxl，626 MB）。**这不是官方镜像**：官方镜像由 Harbor 编排、按 `Dockerfile` 钉死 apt/CRAN/Bioconductor 快照，且官方 agent 是 `claude-code` / `codex` / `opencode` 三个 CLI。
- **验证**：`test_docker_environment_builds_official_layout_command`、`test_docker_environment_file_tools_stay_on_host`（用假 docker 记录 argv，不需要守护进程）；四套件 **121 passed**。
- **容器 vs 宿主（node2+node3，A/D，k=5，recovery=2，wall 240，无注入）**：
  | 指标 | 宿主 expR30 | 宿主 expR35 | 容器 expD2 |
  | --- | --- | --- | --- |
  | A task_correct_mean | 1.00 | 1.00 | 1.00 |
  | D task_correct_mean | 0.80 | 0.90 | **1.00** |
  | A 均值 wall | 38.0s | 31.4s | 36.9s |
  | D 均值 wall | 78.7s | 92.5s | **27.4s** |
  | file_missing 节点数（A/D） | 5 / 4 | 5 / 3 | 5 / 3 |
  | D 均值 input / calls | 27.7k / — | — | 14.7k / 5.8 |
  
  **结论（诚实）**：换成容器后 A 仍是 1.00、`file_missing` 频率也没变（A 5、D 3）——说明「漏写文件」是模型习惯，不是宿主环境造成的。变化在成本：D 的均值 wall 从 78.7/92.5s 降到 **27.4s**（宿主那两轮各含一次 300s+ 的慢调用），token 也降到 14.7k。D 与 A 在本轮**同为 1.00**，仍不能据此说「增强机制提升正确率」。
- **诚实边界**：换成容器只消除了「宿主 shim」这一类环境差异，**没有**得到官方分数（`_grading` 仍缺失），也没有接入官方 agent，因此仍不能与任何官方成绩比较。

## 45. 嵌套形状写坏（`items` 写成空格串）：保持 `valid`，另加交付纠偏

- **现象**（expD4 run0/run1，容器内 padj 注入）：纠偏后模型写出的答案是
  `{"marker_direction":[{"group":"higher in IMF","items":"CD34 PDGFRA"},{"group":"higher in SF","items":"ITGB1 PPARG"}]}`
  —— `items` 是**空格拼接的字符串**而不是字符串列表，且含 ITGB1（说明用的是表内预计算的 `pvalue` 列）。
- **为什么没被拦住**：官方 `_slice_matches_category` 只校验**顶层切片类别**（是不是 list），嵌套形状不校验；`classify_answer` 与之一致 → 判 `valid`。随后 `C001` 走「padj 只出现在探查输出」分支 → `UNVERIFIED/warning`，按 #26 设计不 blocking → 纠偏循环结束。
- **修法（只改交付，不改官方语义）**：新增 `_grouped_items_violation`（[adapter.py](file:///d:/1_yanjiusheng/实习项目/agent/CodeTrail/benchmarks/omicsbench/adapter.py)）——对 `grouped_entity_set` 检查每个分组的 `items` 是否为 `list[str]`；不是则注入一次定向纠偏，要求「把每个 item 拆成一个数组元素」。`outcome` 仍是 `valid`（与官方一致），只是多一次重写机会，走的是与 #24/#39 相同的「交付层纠偏」通道，不占科学纠偏的判定。
- **验证**：`test_grouped_items_written_as_string_get_a_targeted_rewrite`（首轮写字符串 → 提示含 "JSON array of item strings" → 次轮写成列表）；四套件 **122 passed**。
- **真机验证（未完成 → 已用确定性注入补上）**：expD5（容器、D、k=5、padj 注入）第一次运行时 GLM `glm-5.3-flash` **余额不足**（HTTP 429 / code 1113），5 个 run 零模型调用 → 作废；改用 `glm-5.3-flashx` 重跑 **3/5**，但 5 个 run 都没出现 `items` 字符串（交付提示 0 次），因此改用**确定性注入** `--inject-malformed-items`（harness 自己把答案写成 `"items": "PDGFRA CD34"`）跑 expD6：
  - 交付提示**触发 5/5**，最终答案的 `items` **全部变回列表（5/5）**；
  - 但内容只对 **1/5**：4 个 run 把注入里的 ITGB1 原样保留成 `["PPARG","ITGB1"]`（模型只是把字符串拆成数组，没有重算）——**与设计预期完全一致：这条只修交付，不修内容**。
  - 对照 A 组（expD7）同样 **5/5 触发、5/5 修复形状**、dev 3/5 → 交付纠偏**不依赖科学管线开关**，A/D 的内容差异属噪声。
- **可复现命令**：`--inject-malformed-items`（见 `experiment.py` 的 `MALFORMED_ITEMS_SCRIPT`），报告里记 `injected_probe="malformed_items"`。
- **诚实边界**：这条**修不了内容**——上面那两条 run 的 ITGB1 仍会保留（预计算列问题见 #32/#42），所以 dev 可能仍是 0；它只保证「文件至少是可评分的合法结构」。`grouped_ranked_list` 不在此检查范围内（其 item 形态可能不同）。

## 46. 记录型答案写成裸字符串（`differential_feature_set`）：官方顶层校验仍判 valid

- **现象**（expD11，确定性注入，melanoma `n2_knockdown_switch_axis`，容器、A、k=3）：harness 把 `knockdown_axis.json` 写成
  `{"regulatory_axis_effects": ["probe feature A", "probe feature B"]}`。
  官方 `_slice_matches_category` 只要求顶层是 list → 判 `valid`；**三个 run 最终都还是注入值，没有任何纠偏回合**（trace 里模型只跑了一轮），dev 0/3。
- **根因**：#45 同类。`classify_answer` 只校验顶层类别；`_grouped_items_violation` 只覆盖 `grouped_entity_set`；`differential_feature_set` 要求的「每个元素是带 `feature/effect/direction/p_value` 的对象」无人检查，而这份 schema 其实已经印在任务 prompt 里。
- **修法**：新增 `_feature_record_violation`（[adapter.py](file:///d:/1_yanjiusheng/实习项目/agent/CodeTrail/benchmarks/omicsbench/adapter.py)）——对 `differential_feature_set` 要求每个元素是对象且包含答案 schema 的字段（字段名取自公开题 GT 记录的键，**只用字段名、不用值**）；裸字符串/缺字段 → 一次定向纠偏。字段**类型**也按 schema 校验：`effect`/`p_value` 这类数值字段若写成字符串，同样触发纠偏（与 #47 同类）。`outcome` 仍为 `valid`（官方语义不变），与 #45 共用交付层通道。
- **验证**：单测 `test_bare_string_feature_records_get_a_targeted_rewrite`；expD12（同配置，post-fix、容器、A、k=3）：纠偏提示 **3/3 触发**、最终答案 **3/3 变成记录对象**；其中 **1/3 真正重算**（MITF/SOX10/IRF1/IRF4），**2/3 保留注入 sentinel 只补字段**——与 #45 相同的边界：**修交付、不保证内容**。
- **内容诊断（新增 `content_diagnostics`，报告字段，不是分数）**：离线用题目参数（`effect_full_credit_within=0.5`、feature 匹配、direction）核对 expD12 的三个答案——run1 **matched 3/3、effect_within_full 3/3、direction_match 3/3、max |Δeffect| 0.0029、extra_features 1**；run0/2 **matched 0**（仍是 sentinel）。所以内容层面是 **1/3 实质匹配（但多报 1 个 feature）、2/3 只修形状**。**官方是否惩罚多余 feature 未知**，因此不把它写成"1/3 正确"。
- **诚实边界**：① 本地 dev 对 `differential_feature_set` 只有「列表完全相等」的兜底判据，**不能用来判断内容正确率**（expD11/12 的 dev=0 不等于官方 0 分）；② 官方 grader 对 effect/p_value 有容差，本地未实现（同 #42 的边界）；③ 本检查只覆盖「元素不是对象 / 缺字段」，不校验数值口径。
- **可复现命令**：`--inject-malformed-records`（见 `experiment.py` 的 `build_malformed_records_command`），报告里记 `injected_probe="malformed_records"` 与 `injected_probe_node`。

## 47. 数值答案写成字符串（`numeric_tolerance`）：顶层标量校验仍判 valid

- **现象**（expD13，确定性注入，melanoma `n1_differential_expression`，容器、A、k=3）：harness 把 `deg_counts.json` 写成
  `{"up_regulated": "probe number 1", "down_regulated": "probe number 2"}`。
  官方顶层校验把任何非容器值都当 scalar，`"1105"` 与 `1105` 同类 → 判 `valid`；**3/3 run 保持注入值、0 次纠偏**，本地 dev 0/3。
- **根因**：#45/#46 的叶子类型版本。`classify_answer` 只校验顶层类别；字符串数字既不会被协议恢复，也不会被已有的交付检查发现。无论官方 grader 是否做类型强转，这种文件都不可能拿到数值分。
- **修法**：新增 `_numeric_type_violation`（[adapter.py](file:///d:/1_yanjiusheng/实习项目/agent/CodeTrail/benchmarks/omicsbench/adapter.py)）——`numeric_tolerance` 的值必须是 `int/float`（排除 `bool`）；字符串则一次定向纠偏，要求「写不带引号的数字」。`outcome` 保持 `valid`（官方语义不变）。
- **验证**：单测 `test_numeric_answers_written_as_strings_get_a_targeted_rewrite`；expD14（同配置，post-fix）：纠偏提示 **3/3 触发**、最终答案 **3/3 是 JSON 数字**，且 **3/3 是重新计算的数值**（没有把 sentinel 转成数字）。
- **同时修正本地 dev**：`_match` 对 `numeric_tolerance` 按 `full_credit_within`（absolute）判 full credit，不再要求完全相等——expD14 离线重算为 **2/3 full credit**（1039/635、1168/726），1/3 对比度算错（173/110）；expD13 仍 0/3。报告文件保留运行时的旧 dev 值，本段数字是**离线重算**。
- **诚实边界**：① 官方 `numeric_tolerance` 实现不可得；「`full_credit_within` 是 full-credit 容差、`no_credit_beyond` 之外 0 分」来自参数名与题面，未与官方代码核对；② 本地 dev **不建模 ramp 内的部分分**，因此是官方分数的**下界**近似；③ 该检查只覆盖类型，不校验统计口径（#42 的边界仍在）。

## 48. 对比度/分组定义无法从证据验证（已知边界，不修）

- **现象**（expD14 A、expD15 D，melanoma `n1_differential_expression`，scalar-strings 注入，容器、k=3）：两组各出现 **1 次把对比度算成 173/110**（预期 1105/857，容差 ±300 外），其余 run 为 1039/635（容差内）。**D 组的 C001/C002 都没有阻断这类答案**：C001 只判 correction method（run1 PASS、run0/2 UNVERIFIED），C002 scope 全部 `UNVERIFIED`。
- **根因**：`_extract_data_scope` 只识别「all samples / the two X cohorts / eight clones / four IMF|SF clones」等形态；n1 的「two target-silencing groups against the two control groups」不匹配 → 没有 scope 约束。即使提取到，`provenance_check` 也只能看命令文本是否提到范围，**无法证明分组是否正确**。
- **决策**：**不新增约束/判定**——文本证据不足，硬加会产生假阳性（与 #42 同类）。可行方向是把「分组列的可核验摘要（每组样本数 / 对比矩阵）」写进证据，属未来工作；在此之前，这类内容错误只能在 dev/离线核对里看到。
- **诚实边界**：A 与 D 都是 2/3（n=3），且同样的对比度错误在两组各出现一次 → **没有证据表明科学管线提升了该题的内容正确率**。

## 结果三分类

- **实际运行结果**：
  - expR5（重构前）：node3 单点 A，`valid`/dev=1.0，usage `19184/1253`/7 calls。
  - expR6（本轮改动后）：node3 单点 A，`valid`，wall 81.58s，usage `34623/2822`/12 calls；但 dev=0.0——模型写的是 `{"origin": "myogenic"}`，不在官方 choices（`satellite (myogenic) cells` 等 4 项）中。**这是内容错误，不是 schema/adapter 问题**；也说明「outcome=valid」只代表能被官方评分，不代表答对。
  - expR7（本轮改动后）：C 组（constraints+verification）node2 单点，`valid`/dev=1.0，wall 34.31s，usage `19635/863`/7 calls；verification 真实产出 4 条：C001/C002/C003 `UNVERIFIED`（warning，基于命令文本无法证明方向/范围），`answer_provenance` `PASS`。无 blocking，未触发 recovery。
  - expR8（本轮改动后）：D 组（constraints+verification+recovery）node3 单点，`valid`/dev=1.0，wall 37.87s，usage `16960/1230`/6 calls。**recovery 真实触发并成功**：第 1 次 run 只执行了 4 条 `run_shell`，模型在 `<final>` 文本里宣称“已写入 imf_origin.json”但实际没有调用 `write_file` → 判 `file_missing` → 恢复提示要求写文件；第 2 次 run 用 1 次 `write_file` 写入正确选项（mtime 与恢复步一致）。verification 仍为 3 条 `UNVERIFIED`(warning) + `answer_provenance` `PASS`，无 blocking。
  - expR9（#24 之后，A 组 node3）：`valid`/dev=1.0，wall 79.6s，usage `26131/2976`/9 calls。再次出现同样的 tool-use 模式：第 1 次 run 的 6 次工具全是数据检查，答案只写在 `<final>` 文本里；`file_missing` 恢复后第 2 次 run 用 `write_file` 写对。**#24（菜单外选项重写）本次未在真机触发**——模型直接答对了选项，因此该修复只有离线证据。
  - expR10（k≥5 稳定性，A 组 node2+node3，k=5）：node2 **5/5 全对**；node3 **4/5**（run4 答成 `fibro-adipogenic progenitors (FAPs)`，`detail=wrong`，菜单内选错，仅 1 次工具调用、17s）。task_correct_mean=0.9。**结论：node3 不是稳定全对**，属模型语义判断波动，无法在不改 prompt 的前提下修复。
  - expR11（漂移闭环，node1，A vs D，k=3，wall 300s）：**6/6 全部 `timeout`，0 个答案文件**。根因不是科学能力，而是 #25 的脚本路径未翻译——模型写出正确脚本却每次崩在 `/data` 不存在，随后在环境探测上耗尽 40 步。D 组 run1 的 `C001 VIOLATION` 还暴露了 #26 的假阳性（仅打印 `padj` 列被当成用 padj 筛选）。**本轮无法据此判断漂移纠偏是否有效**。
  - expR12（#25/#26 修复后，node1，A vs D，k=2，wall 240s）：**答案产出从 0/6 恢复到 3/4**。A 组 2/2 `valid` dev=1.0（146s/100s）；D 组 run1 `valid` dev=1.0（67s），run0 仍 `timeout`（248s，24 calls，无答案→暴露 #27 恢复预算被吃光）。**#25 的效果得到真机确认**：node1 从「全崩在环境探测」变成「能跑通脚本并写对答案」。
  - expR13（#27 修复后，node1 D 组，k=3，wall 240s）：**2/3 `valid` dev=1.0，0 次 timeout**（expR12 是 1/2 且含 timeout；expR11 是 0/6）。run0 65.8s：首次尝试 8 次工具就把答案写在 `<final>` 文本里、无文件 → 恢复提示实际由 `answer_provenance`(blocking)+写文件指令组成 → 恢复 run 一次 `write_file` 写对（早期退出使该 run 标记 `Run closed by caller`）。run2 150.8s 同样 `valid`/1.0。run1 231.9s → `schema_mismatch`：恢复写成「基因→方向」字典而非分组列表，唯一恢复额度用尽（暴露 #28）。**#27 的效果得到真机确认：timeout 消失，恢复阶段确实能写答案。**
  - expR13 顺带验证了 #23：运行中 `report.json` 已存在且 `partial=true / summary=null / runs=1`，进程中断不再丢已完成 run。
  - expR14（#28 恢复提示带结构 + `recovery-attempts=2`，node1 D 组，k=3，wall 300s）：**3/3 `valid` dev=1.0，0 次 timeout，0 次 schema_mismatch**（169.8s/77.9s/80.2s；run0 首次尝试 17 次工具直接写对答案；run1/run2 各触发一次恢复，恢复 run 的首个模型回复即写出正确分组列表，随即因答案有效提前关闭）。对比 expR13（同节点、recovery=1）：2/3 + 1 次 schema_mismatch。**注意**：expR13 与 expR14 同时变了 wall(240→300) 与 recovery(1→2)，且 n=3，不能把提升单独归因给 #28；只能说「#28 的失败模式（写错结构后无第二次机会）在本次 3 个 run 中未再出现」。
  - expR15（A vs D 对照，node2+node3，D 组 k=5，wall 180s，与 expR10 同参数）：D 组 node2 **5/5**、node3 **4/5**，task_correct_mean **0.9**，与 A 组（expR10：node2 5/5、node3 4/5、0.9）**完全一致**；0 timeout、0 failed。D 组 10 个节点全部 `answer_provenance=PASS`，C001–C003 均为 `UNVERIFIED/warning`，**没有任何 blocking**，recovery 一次也没触发。**诚实结论：这次对照既没测到「增强机制提升正确率」，也没测到它拖累正确率**——不是因为它无效，而是这两个节点上流水线处于「无违规可纠」的空转状态，无法回答「有漂移时是否有用」。
  - 「漂移闭环真机状态」：自然条件无漂移（#30）；人为注入后**检测/注入 3/3 有效，改对 1/3→2/3**（#33）。
  - expR16 / expR17（padj 诱饵探针，非官方题）：v1 A 2/3、D 2/3（各含 1 次 `schema_mismatch`/`timeout`）；v2 A **3/3**、D 2/3（+1 `timeout`）；**两版都未诱发 padj 漂移**，`C001` 恒为 `PASS/UNVERIFIED`，recovery 未触发。详见 #30。
  - expR21（client 层注入尝试，失败）：两版 client shim 都几乎不触发——模型多数情况下先写出合法答案，adapter 在它返回 `<final>` 之前就提前退出，注入没有机会执行。据此改为 adapter 层注入（#33）。
  - expR24（#34/#35 后，受控注入，D 组 k=5）：**检出 5/5**（`C001 VIOLATION/blocking`）、**恢复 5/5** 注入；最终**内容正确 3/5**（run3/run4 完全正确；run0 内容正确但多写了一个 `method` 键 → `schema_mismatch`），**错误 2/5**（仍用表格 `pvalue` 列，含 ITGB1）。**结论：检测稳定；修复不稳定（≈60%），且失败模式已定位**——(a) 换用预计算列（#32 提示只能部分缓解），(b) 纠偏后多写键导致形状错误（恢复额度已用尽）。
  - 汇总：expR22 1/3 → expR23 2/3 → expR24 3/5（内容口径）。样本都很小，只能说「有改善迹象，未达稳定」。
  - expR25（recovery=2，受控注入，D 组 k=5，wall 240s）：**检出 5/5、注入纠偏 2 次/run（共 10 次），但内容改对仅 1/5（task_correct_mean=0.20），0 失败 run**。表面看是「恢复额度加倍反而变差」，实际根因是 #37——纠偏回合被注入留下的旧有效答案提前关闭，2 次纠偏等于 2 次空转。**这是一次典型的「指标在骗人」：只看分数会误判为机制无效，逐条读 session 才发现测量与执行都被同一个 bug 破坏。**
  - expR26（#37 修复后首次复跑，受控注入，D k=5 计划）：run0 已足够定位问题——纠偏回合不再被旧答案提前终止（#37 生效），第二次纠偏真的重算了，但写到了自拟的 `answers/answer.json`（#38），受评文件未变 → dev=0.0。因代码继续变动，**该轮主动中止，不纳入统计**（避免同轮混合版本）。
  - expR27（#37+#38 后，受控注入，D 组 k=5，recovery=2）：**2/5 完全正确（task_correct_mean=0.40）**，0 失败 run；checkpoint 后的纠偏确实重写了受评文件（`C001` 由 VIOLATION 转 PASS），但 exposed #39：run0 成员集合正确、分组标签被改写成 `up_in_IMF`/`up_in_SC` → dev=0（旧判据无标签校验，不会触发纠偏）。
  - expR28（#39 后，受控注入，D 组 k=5，recovery=2）：**2/5 完全正确（0.40）**，0 失败 run；`output_labels` 校验**真实抓到 1 次标签漂移**（run2 写成 `concordant`/`discordant` 两组、共 7513 个基因——完全脱离题面的 7 个 marker 面板），说明新判据可用；run1 `invalid_json`、run4 未重写（`C001` 仍 VIOLATION，wall 376s 超出 240s 预算，属 #16/#22 已知边界）。**残余失败全部是模型侧行为（自拟标签/文件名、写坏 JSON、不重写），不再是 harness 看不见。**
  - 注入系列汇总（D、k=5、recovery=2）：expR25 1/5 → expR27 2/5 → expR28 2/5。**n=5 不能宣称「机制变好」**；能站住的是：检测稳定（每轮 5/5 检出注入的漂移）、纠偏回合真的在执行、以及 #37/#38/#39 都是确定性缺陷且已被测试覆盖。与 expR24 的 3/5 也不可直接比较（判定口径增加了标签校验，且 expR24 的「改对」按内容计）。
  - **expR29（recovery=3 + schema/标签模板，受控注入，D 组 k=5，wall 240s）**：**5/5 完全正确（task_correct_mean=1.00），0 失败 run**，均值 wall 106.5s。逐 run 核对：每个 run 都是「首次尝试 + **1 次**纠偏回合」，即**第 2/3 次纠偏额度未被使用**；`C001` 5/5 由 VIOLATION 转 PASS（且 `PASS` 分支要求执行证据里存在检验调用，说明是重算而非换列），`output_labels` 5/5 PASS，最终答案都是 `higher in IMF: PDGFRA/CD34` + `higher in SF: PPARG`（注入版本含 ITGB1）。
  - **归因（谨慎）**：expR29 同时改了三处（recovery 2→3、模板、标签判定收窄为「只罚自拟标签」），n=5、单节点单条件，**不能把 5/5 归给任何单项**。但可以确定两点：① 由于每 run 只用了 1 次纠偏，**额外额度不是原因**；最可能的原因是 #40 的「schema+标签模板」让纠偏一次就对齐交付格式；② 标签判定收窄并没有把错答案放行（expR28 run2 的 `concordant` 仍会 VIOLATION）。
  - 注入系列汇总（D、k=5）：expR25 1/5（recovery=2，受 #37 破坏）→ expR27 2/5（recovery=2）→ expR28 2/5（recovery=2 + 标签校验）→ **expR29 5/5（recovery=3 + 模板）**。仍受 n=5、单题单节点限制，只能作为「修复有效的正向证据」，不构成泛化结论。
  - **expR30（反向验证：无注入 A vs D，node2+node3，k=5，wall 240s，recovery=2）**：A **10/10 节点全对（task 1.00）**，均值 wall 38.0s、input 19.6k/run；D **8/10（task 0.80）**，均值 wall 78.7s、input 27.7k/run。两处 D 失败均为模型侧：run0 node3 选了菜单内但语义错误的 `endothelial progenitor cells`（与 expR10 run4 / expR15 run1 同类波动）；run1 node2 在 `file_missing` 纠偏后把答案扩大到候选列表之外（#41）。**没有任何一次「本来正确、被纠偏改坏」**——这是本次反向验证最关键的结论。
  - 反向验证的诚实边界：n=5、单题、D 比 A 多花约 1.9–2.1×（wall/input tokens），且 D 的 0.8 vs A 的 1.0 与历史 expR10/expR15（均 0.9）处在同一波动区间，**既不能证明 D 更好，也不能证明 D 更差**；能站住的是「模板没有破坏正确答案」和「多花的成本来自额外校验/偶发纠偏」。
  - expR31（#41 后，node2 单点 D，k=5，recovery=2）：**5/5 全对（task 1.00）**，0 失败，均值 wall 39.8s；逐 run 核对纠偏提示，只有 1 次 `file_missing` 纠偏、**没有一次多选越界事件**，所以 5/5 不能归功于 #41（与 expR30 D 的 4/5 差异属模型波动）。
  - **换题验证（expR32/expR33）**：把 melanoma 数据从 HF 取回（`gene_expression_tpm.xlsx` / `sample_metadata.csv`，sha256 与题目 JSON 完全一致），按 `/data` 扁平映射落到独立 data-root 后跑 node6。结论分两层：① **expR29 的 padj 通道无法跨题复验**——其余公开题的统计口径是 BH 或 Bonferroni（方向相反），且 `melanoma n1/n2` 的答案是数值/记录型，漂移在文本证据里看不见（#42）；② 能跨题复验的是**交付通道**：菜单外选项注入后 5/5 触发纠偏、4/5 清除（expR33），其中 1 次因 wall 用尽未完成。
  - **expR34（#43 后复跑，同 expR29 配置：D、k=5、recovery=3、padj 注入）**：**5/5 完全正确（task 1.00），0 失败**，均值 wall 109.0s。与 expR29 合并为 **10/10**（两次独立 k=5），说明该条件下的结果**可复现**；仍限单题单节点。注：run0/2/4 的 `C001=UNVERIFIED` 是因为模型在**探查阶段**打印了 `padj` 列（`_has_adjust` 命中但不在选择比较里），按 #26 的设计判 warning 而非 blocking，最终 dev 仍为 1.0。
  - **expR35（#43 后，自然条件 A/D、node2+node3、k=5、recovery=2）**：A **10/10 节点全对（1.00）**、均值 wall 31.4s；D **8/10（0.90）**、均值 92.5s（run1 node2 为 323s 慢调用超时）。`file_missing`：涉及节点 A 5 / D 3（基线 expR30 为 5 / 4），**同节点连续两次漏写 0 次**；#43 回贴提示 8/8 出现。详见 #43。
  - **expR36（#43 + `--wrapup-after-tool-calls 8`，同条件）**：A **0.90**（run3 node3 选错）、D **1.00**；均值 wall 49.2s / 38.4s。`file_missing` 涉及节点 **13**（A 6 / D 7），比不加 wrapup 时更多；wrapup 提示在 10 个节点 run 中只触发 **1 次**——阈值 8 高于多数回合的实际工具调用数（4–7），因此这个旋钮对「首轮漏写」基本无效甚至略差（n 小，不作因果结论；D 的 1.00 同属波动）。
  - **expD0（容器冒烟，node2，A，k=1）**：`valid`、dev=1.0、wall 16.9s —— Docker 后端端到端可用（`/data` 只读挂载、pandas 读到 18872×19、`/workspace/answers` 可回读）。
  - **expD1（容器内复跑 expR29 配置：D、k=5、recovery=3、padj 注入）**：**5/5 完全正确，0 失败**，均值 wall **53.0s**、input ≈21.7k/run；对比宿主同配置 expR29/expR34 的 106.5s / 109.0s、input ≈46k/run —— 容器里**更快且更省 token**（工具调用 6–9 次 vs 宿主 12–21 次），因为不再需要路径翻译与 Linux 工具绕行。**这是宿主 shim 开销的直接证据，不是能力提升**（两边都是 5/5）。
  - **expD2（容器内 A vs D，node2+node3，k=5，无注入）**：A **10/10（1.00）**、均值 36.9s；D **10/10（1.00）**、均值 **27.4s**、input 14.7k/run；两边 `file_missing` 节点数 A 5 / D 3，与宿主 expR35 完全一致。**容器消除了 D 在宿主上的 wall 长尾（78.7/92.5s → 27.4s），但没有改变正确率排序**（A 与 D 同为 1.00）。
  - **容器内 3 轮 padj 注入（D、k=5、recovery=3、wall 240）**：expD1 **5/5**（均值 wall 53.0s）、expD3 **5/5**（40.1s）、expD4 **3/5**（43.8s）→ **合计 13/15（86.7%）**，均值 45.6s。宿主同配置 expR29/expR34 = **10/10**；宿主+容器合计 **23/25（92%）**。
  - expD4 的两条失败是 #45：纠偏后 `items` 写成空格串且含 ITGB1（用了表内 `pvalue` 列），`valid` + `C001=UNVERIFIED` → 不再纠偏。**这两条不是环境问题**，容器与宿主都会出现同类内容错误（宿主 expR22–24 已记录过换用预计算列）。
  - **expD5（容器、padj 注入、D、k=5）**：`glm-5.3-flash` 因**余额不足**（429/1113）导致首轮作废（0 模型调用、只剩注入探针）；改用 `glm-5.3-flashx` 后 **3/5（0.60）**，均值 61.6s。两条失败：run1 写出 **3095/1322 个基因**的退化答案（45KB），run4 的 SF 组含 ITGB1（预计算列）。**#45 的交付提示 5 个 run 都没触发**（没有出现 `items` 字符串）。另外 flashx 在这 5 个 run 里 **0 次 file_missing**（flash 是每次 3–5 次），说明模型切换的差异远大于环境差异，**不能与 flash 的历史数字混在一起比较**。
  - **expD6（#45 确定性注入：harness 把 `items` 写成空格串，容器、D、k=5、flashx）**：交付提示 **5/5 触发**，最终 `items` **5/5 变回列表**；但 dev 只 **1/5**——4 个 run 把注入里的 `ITGB1` 原样保留（只拆串、未重算）。**这是 #45 的预期效果边界：修交付、不修内容**。
  - **expD7（同注入的对照组 A，容器、k=5、flashx）**：交付提示 **5/5 触发**、形状 **5/5 修复**，dev **3/5**（2 个 run 保留 ITGB1）。**说明交付纠偏与 A/D 分组无关**（它本就属于交付层、对所有组开启）；A(3/5) 与 D(1/5) 的内容差异在 n=5 下只是噪声，不能读成「加科学管线让内容变差」。
  - **expD8（复跑 A+D 各 k=5，同注入、容器、flashx）**：A **1/5**、D **2/5**；形状仍然全部是列表。
  - **#45 注入系列汇总（expD6 D + expD7 A + expD8 A/D，共 20 个 run）**：**交付提示 20/20 触发、最终 `items` 20/20 是列表**；内容正确 A **4/10**、D **3/10**（失败几乎都是保留 ITGB1）。**结论：交付纠偏稳定复现 20/20；内容正确率与分组无关、落在噪声区间，不能宣称任何一方更好。**
  - **默认恢复额度调整（本轮，代码改动）**：`benchmarks/omicsbench/experiment.py` 与 `smoke.py` 的 `--recovery-attempts` 默认值由 **2 → 3**（此前 expR29/expR34/expD1+ 均为显式传参）。理由：expD4/expD6 中一次失败节点可能连续消耗「形状纠偏 + 内容纠偏」两回合；固定 2 会让第三次失败直接落到 `valid but wrong`。**这是参数默认值调整，没有新证据表明它会提高正确率**；四套件复跑 **122 passed**。adapter 库层默认仍为 1，不受影响。
  - **`glm-5.3-flash` 可用性探测（本轮）**：用 `BIGMODEL_API_KEY` 直连 `open.bigmodel.cn/api/paas/v4` 发最小请求，`glm-5.3-flash` 仍返回 **HTTP 429 / code 1113「余额不足或无可用资源包」**；同 key 的 `glm-5.3-flashx` 返回 200。因此「充值后用原模型重跑 expD6/D7 以对齐历史 flash 数字」**仍被外部账户余额阻塞，未执行**，也不能把 flashx 的数字当作 flash 的复现。
  - **expD9（换题+换环境：melanoma `n6_pathway_enrichment`、菜单外选项注入、容器、A/D 各 k=3、flashx、wall 360）**：注入命令 **6/6 全部执行**（trace 里 6 个 run 都出现 `Wnt signaling pathway`，该值不在题目提供的 pathway 菜单内）。**菜单外值被清除 5/6**：A 3/3 清除（Focal/ECM/ECM，均在菜单内）、D 2/3 清除（ECM、Focal）；剩下 1 个 D run 最终仍是 `Wnt signaling pathway`，其 wall **370.6s > 360s 预算**（同 expR33 那次未完成，属预算而非判定失效）。正确率 A **2/3（0.667）**、D **1/3（0.333）**，均值 wall 222.3s / 303.5s——**n=3、且 3 条错误里 1 条是超预算未完成，不能读成组间差异**。这条给 #24 的跨题证据加了「容器 + 新模型」两个维度，同时说明**换环境不会改变交付通道的行为**。
  - **#45 无法迁移到第二道题（本轮核查，未开跑）**：公开题里另有 `grouped_entity_set` 节点的只有 `adult_kidney_pt_vcam1_state`（3 个节点），但其数据是 h5ad + fragments，HF 仓库对当前凭据返回 **401（需授权）**，且现有 `omicsbench-runtime:dev` 镜像未装 scanpy/anndata。因此 malformed-items 探针**仍只有 bovine 一道题可跑**，#45 目前仍是「单题 20/20」证据，不能声称跨题泛化。
  - **探针通用化（本轮，代码改动）**：菜单外探针不再是写死 melanoma 的常量，改为 `build_offmenu_probe_command(question, node_ids)`：按 `--nodes` 选定第一个 choice 节点（优先 `multi_select_choice`），从题目 `choices` 取菜单内首项（对多选保留一项、再加固定 sentinel `probe off-menu value`），写入该节点的 `output_filename`；报告新增 `injected_probe_node`。若筛选后的节点里没有 choice grader，CLI 直接报错退出而不是静默空跑。**注意**：expD9 用的是写死的 `Wnt signaling pathway`，expD10 用通用 sentinel，两者数值不可逐字对比。
  - **expD10（探针通用化后：bovine `lineage_regulator_selection`、multi_select 菜单外注入、容器、A/D 各 k=3、flashx、wall 200）**：这是 #41（多选越界）的确定性复现——注入 `["PRDM16", "probe off-menu value"]`，PRDM16 在菜单内但不在答案里，sentinel 在菜单外。trace 核对：注入 **6/6 执行**、纠偏提示 **6/6 出现**、最终答案 **6/6 完全正确**（`{MYOD1, MYOG, MYF5}`）——模型不是只删掉越界项，而是重新计算得到正解。A 均值 wall 30.3s、D 20.4s。**诚实边界：每组 n=3、单节点且该节点较简单，不能读成 D 优于 A**；能说的是 #41 的纠偏在「多选 + 菜单外项」上可确定性复现，且同一开关已能覆盖两种题型。
  - **测量补丁：`timed_out` 标记（本轮，代码改动）**：expD9 的 D run1 在 370.6s（>360s 预算）时仍未清掉菜单外答案，但报告里 `outcome=valid`，与「模型主动选错」无法区分。现在 `NodeSubmission` 新增 `timed_out`，报告每个节点带该字段：**`outcome` 仍按官方语义**（valid = 文件可交给 grader），但 `timed_out=True` 明确标注「纠偏没跑完」。新增单测覆盖「valid + 超时」与「outcome=timeout」两条路径。
  - **expD11（#46 的修复前诊断，melanoma n2、malformed-records 注入、容器、A、k=3）**：注入后 **3/3 run 保持 `valid`、最终答案仍是裸字符串列表、0 次纠偏**（每个 run 只有一轮 trace）。这就是 #46 的缺陷证据：官方顶层形状通过，但记录形状无人检查。
  - **expD12（#46 修复后，同配置）**：纠偏提示 **3/3 触发**、最终答案 **3/3 变成记录对象**；1/3 真正重算（MITF/SOX10/IRF1/IRF4），2/3 保留了注入 sentinel 只补齐字段。**与 #45 一样：交付修复稳定、内容不保证。** 注意 dev 对此题型只有「完全相等」兜底判据，**expD11/12 的 dev=0 不能当作官方 0 分**；改用新的 `content_diagnostics`（参数级、非分数）核对：run1 matched 3/3、effect 3/3 在容差内、direction 3/3、max |Δeffect| 0.0029、extra=1；run0/2 matched 0。
  - **expD13（#47 修复前诊断，melanoma n1、scalar-strings 注入、容器、A、k=3）**：3/3 `valid`、最终仍是字符串数字、**0 次纠偏**；本地 dev 0/3。缺陷证据成立（官方顶层 scalar 校验放行）。
  - **expD14（#47 修复后，同配置）**：纠偏提示 **3/3 触发**、最终 **3/3 是 JSON 数字且为重新计算的数值**；按参数化 tolerance 离线重算 dev：**2/3 full credit**（1039/635、1168/726），1/3 对比度算错（173/110）。报告文件里的 dev 是运行时旧判据的值（0/3），两者不要混用。
  - **expD15（D 组同配置对照，melanoma n1、scalar-strings、容器、k=3）**：dev **2/3**（1039/635 两次）、1 次 **173/110**（与 expD14 同型的对比度错误）；C001 `PASS` 1 次 / `UNVERIFIED` 2 次，C002 scope 全 `UNVERIFIED`，无 blocking。**A（2/3）与 D（2/3）在 n=3 下没有差异**，同样的对比度错误两组各出现一次；均值 wall A 178.3s / D 138.6s（噪声）。这是 #48 的证据：科学管线能看方法/口径线索，但看不到「分组定义是否正确」。
  - **expD16（自然条件整题基线：melanoma 全 DAG、A、k=1、容器、无注入、wall 360/节点）**：总 wall 353.4s、30 calls；节点结果 n1 **1.0**（1039/635，在 ±300 内）、n2 **dev 0 但 `content_diagnostics` 显示 3/3 feature 匹配、effect 3/3 ≤0.5、direction 3/3、max |Δ| 0.0029、extra 0**、n5 **3/3 匹配 + 1 个多余基因（ITGA1；该字段为离线重算，运行时尚未包含 gene_set 诊断）**、n6 选了菜单内但错误的 `Cell adhesion molecules`、n4 答 **0**（GT 194）。n4 trace 显示失败链：DE 结果用 **Ensembl ID（ENSG…）**，而 GO/GAF 注释用 **HGNC/UniProt 符号** → 交集 0（`annotated genes total: 0`），随后两个补丁脚本报错（IndexError/ValueError），最终仍写出 0 并通过 `valid`。**这是模型侧的内容/命名空间错误，不是交付或 schema 问题**；也再次说明 `dev=0` 对 n2 这类题型会被精确相等判据误导。仅 k=1，不能泛化。
  - **expD17（同整题 D 组对照，k=1、容器）**：总 wall 352.9s；n1 **974/565**（±300 内）、n2 3/3 匹配（extra 0）、**n6 选对 `Focal adhesion`**、n4 **47**（GT 194；超过 60 的全优容差，落在 250 的零分界内）、n5 3/3 + 2 个多余（ITGA1、ITGA11）。任务均值 0.4 vs A 的 0.2，但 **k=1，且唯一节点级差异是 n6（选择题），不能归因于科学管线**；C001 在 n1/n2 为 PASS，C002 scope 仍 UNVERIFIED。
  - **expD18（A、k=2）/ expD19（D、k=2）**：A 两次 task 0.2 / 0.6（均值 wall 402.3s）、D 两次 0.2 / 0.6（均值 wall 542.3s）。D 的 n6 两次都选 `Focal adhesion`，A 的 n6 三次分别是 Cell adhesion / Proteoglycans / ECM-receptor。
  - **整题 A vs D 合计（A = expD16+D18，D = expD17+D19，各 n=3、同一模型、容器、无注入）**：n1 **3/3 vs 3/3**；n2 **内容均 3/3 匹配**（`content_diagnostics`；dev 0 来自精确判据）；n4 **A 1/3（159）vs D 0/3（47,47,0）**；n5 **各 1/3**（多余基因数 A 1/1/0、D 2/2/0）；n6 **A 0/3 vs D 2/3**；节点 dev 均值 **A 0.333 / D 0.400**，均值 wall **386.0s / 479.2s**。**n=3，不能宣称 D 更好或更差**；n6 是唯一明显的节点级差别，值得扩大 n 验证（expD19 的两个 n6 trace 里 **0 次纠偏提示**，说明差异来自模型首轮选择，不是纠偏闭环的功劳）。n2 的 p_value 与 GT 差很多（如 4.65e-09 vs 0.0142），官方 `p_value_no_credit` 语义未知，本地诊断未计入。
  - **expD20（n6 单点、A、k=5、容器、无注入）/ expD21（n6 单点、D、k=5）**：A **3/5**（`Focal adhesion`×3）、1 次菜单内错误（PI3K-Akt）、1 次在 361.6s 写出**菜单外** `KEGG_CYTOKINE_CYTOKINE_RECEPTOR_INTERACTION` 且 `timed_out=True`（纠偏没跑完）；D **1/5**（Focal adhesion×1）、1 次菜单内错误（ECM-receptor）、**3 次超时且没有答案文件**（364.6/363.4/364.7s，`answer_provenance=VIOLATION`）。均值 wall A 289.4s / D 285.6s。
  - **n6 单点条件的解读（谨慎）**：该节点单独运行时接近 360s 预算（一次跑完要 200–365s），结果主要由调用延迟决定；且 A/D 两组并行执行，**不能把 D 的 3 次超时归因于科学管线**。官方 DAG 中 n6 在 n1 之后运行，单点 n6 只是压力条件。**整题（A 0/3 vs D 2/3）与单点（A 3/5 vs D 1/5）方向相反**，两者都落在噪声/预算效应内，**不构成任何优劣结论**。新增的 `timed_out` 在这里体现出价值：A run1 的菜单外答案与 D 的 3 个无答案 run 都被明确标注。
  - **expD22（bovine 整题、自然条件、A、k=2、容器、wall 240）/ expD23（同 D、k=2）**：A 的 marker 节点 **0/2**（run0 `IMF=PDGFRA/PPARG/CD34, SF=[]`；run1 `IMF=PDGFRA/DLK1/ZNF423/CD34, SF=PPARG/PDGFRB`），node2/node3 均 2/2，task **0.667**；D 的 marker **2/2**（`IMF=PDGFRA/CD34, SF=PPARG`，与 GT 完全一致），三节点全对，task **1.0**。**n=2**；A 的两次失败都是**内容错误**（多报/错分组），不是交付问题，**不能据此说 D 更好**。
  - **expD24 / expD25（同题 A/D 各 k=4，本意扩到 n=6）：整轮作废**——所有 run 的每个节点都返回 `HTTP 429 / code 1113「余额不足或无可用资源包」`，**0 次模型调用、无任何答案**。这两轮**不计入任何统计**；同时说明 `glm-5.3-flashx` 的余额也已耗尽，真实运行在充值前无法继续。
  - **报告修正（本轮，代码改动，属 #20 的延伸）**：一次 run 里**所有节点都因同一错误失败**时（如上面的 429），报告会把该 run 的 `error` 置为 `all nodes failed: ...`，并计入 `failed_runs`；此前这种 run 会以「干净的 0.0 + failed_runs=0」出现。单测 `test_experiment_marks_a_run_whose_nodes_all_failed`。
  - **离线语料盘点（本轮，仅统计既有 68 份 report，不比较性能）**：共 **303 个 run / 461 个节点**；节点结果 `valid` 387（84%）、`timeout` 41（8.9%）、`error` 24（5.2%）、`schema_mismatch` 8、`invalid_json` 1。24 个 `error` **全部是 expD24/25 的 429 余额失败**；8 个 `schema_mismatch` + 1 个 `invalid_json` 出现在早期实验（exp7/expR13/16/24/28），最近的 expD 系列没有新的坏形状；`timed_out` 字段（新加）共 4 个节点，都在 expD21（n6 单点预算耗尽）。**这批 report 混有不同模型、不同 prompt 版本与注入条件，只能当"语料库存"，不能横向比较成功率。**
  - 本轮复探模型可用性：`glm-5.3-flashx` 与 `glm-5.3-flash` **均返回 429 / code 1113**，真实运行继续被阻塞（第 2 轮）。
  - 官方计分包结论（尝试获取，失败）：`pip index versions omicsbench` / `omicsbench-runner` 均无匹配发行版；本地源码副本 `tmp/omicsbench_upstream` 中 `omicsbench/grading.py` 只是 `_grading.*` 的再导出，`validate.py` 直接 `from ._grading.databases.lookup import ...`，而 `_grading/` 目录**整个缺失**（该副本也不是 git 仓库，无法从远端补取）。因此**官方计分无法运行**；本地能对齐的只有题目 JSON（`questions/bovine_imf_sf_preadipocyte_origin.json`）里的 grader 类型（`grouped_entity_set` / `multi_select_choice` / `multiple_choice`）、参数与 `ground_truth`。dev 分数仍是**近似实现**，不等于官方成绩。
- **自动化测试结果**：`tests/test_omicsbench_adapter.py + test_omicsbench_scientific.py` → **80 passed**（覆盖 #1–#4、#6–#35）；四个相关套件（含 `test_verifier.py`、`test_scientific_gate.py`）在 #36–#45 后合计 **122 passed**；本轮（探针通用化 + `--recovery-attempts` 默认 3 + `timed_out` 标记 + #46 记录形状/字段类型 + #47 数值类型 + tolerance-aware dev + `content_diagnostics`（记录/基因集）+ 全节点失败可见性）新增 15 个用例后 **137 passed**。
  - 全量 `pytest -q`（#36–#40 后，临时目录重定向到 D 盘）→ **1215 passed / 0 failed**（19m15s）；#41 之后同一命令 → **1215 passed / 1 failed**（19m49s）。失败是既有 flaky `tests/test_privacy.py::test_web_intake_persistence_masks_canary_without_losing_its_label`（`pico.web.RequestError: 任务不存在或服务已重启`），**单独复跑 2.34s 通过**；该用例在本轮三次全量中 2 次通过、1 次失败，与 #41 无关（未触碰 `pico/web.py`、`privacy`）。更早记录：默认临时目录（C:）时 **1189 passed / 16 failed**，全部为 `sqlite3.OperationalError: database or disk is full` 与 `privacy_state_unavailable`（C: 仅余 ~5GB）。
  - 其后只新增了开发用 CLI 探针（`--inject-offmenu-choice` + 报告 `injected_probe` 字段），未改动判定逻辑；相关四套件复跑 **118 passed**，未再重复全量。
- **未验证假设**：
  - 「增强机制提升正确率」——已在 node2+node3 做 A vs D 各 k=5（expR10/expR15）：两者 task_correct_mean 都是 0.9，且 D 组无任何 blocking，属空转对照，**不能据此声称有效或无效**。
  - 「增强机制拖累正确率/成本」——同样未被证实；D 组 wall（均值 67.8s）高于 A（34.7s），但方差大（29.8–136.2s），且无 blocking，不能归因于流水线开销。
  - 「method drift 纠偏能把答案改对」——受控注入（#33）后检出 5/5 稳定；「完全正确」在 recovery=2 时为 1/5 → 2/5 → 2/5（expR25/27/28），加上 #40 模板与 recovery=3 后为 **5/5（expR29）+ 5/5（expR34）= 10/10**。**仍不能说「稳定修复」到可泛化的程度**：单一题目/节点/条件，且 expR29 三处改动同时上线；能说的是「该条件下结果可复现，且已定位的格式/测量缺陷修完后首次纠偏即改对」。残余风险仍是模型行为（写坏 JSON、不重写、越过预算的慢调用）。
  - 官方计分口径——无法核对（见「官方计分包」一条）；`grouped_entity_set` 的 `item_beta` 具体含义、`multiple_choice` 对近义/缩写是否给分，都只有本地近似判定。
  - #28 是否真的提升了结构正确率——expR13→expR14 同时改了 wall 与 recovery 次数，n=3，无法单独归因。
  - #24 的「菜单外选项重写」已在 expR32/expR33（换题：melanoma node6，注入菜单外选项）真机复现：5 个 run 都触发纠偏、4 个清除菜单外值（1 个因 wall 用尽未完成）；只 1 个选对 pathway（语义问题，非格式）。expD9 在容器 + flashx 下换题复验：注入 6/6 触发、清除 5/6（1 个 D run 因 370.6s>360s 未完成）；expD10 在 bovine 多选节点上 6/6 清除并重算正确。
  - `differential_feature_set` 的**内容正确率**未验证：本地 dev 只有「列表完全相等」兜底判据（官方还有 effect/p_value 容差），expD11/expD12 只能证明交付形状通道；`content_diagnostics` 显示 expD12 run1 的 3 个期望 feature 全部匹配且 effect 在 0.5 内，但多报 1 个 feature——**官方是否对多余 feature 扣分未知**，故不能写成"正确"（#46）。
  - `numeric_tolerance` 的本地 dev 已按 `full_credit_within` 近似（不建模 ramp 部分分，是官方分数的下界），但**未与官方代码核对**；expD14 的 2/3 是本地口径（#47）。
  - 「科学管线提升内容正确率」——expD14/expD15（A vs D、melanoma n1、k=3）都是 2/3，且对比度错误各出现一次；**n=3 且存在同样失败模式，不能支持该结论**（#48）。
  - 整题级 A vs D（各 n=3、无注入）：节点 dev 均值 **A 0.333 / D 0.400**，唯一的明显节点差别是 n6（**A 0/3 vs D 2/3** 选对 pathway）。样本太小、同一模型、且 D 多花约 24% wall（479s vs 386s），**不能据此证明科学管线有效**。n6 单点扩到各 k=5 后方向又反过来（**A 3/5 vs D 1/5**，D 有 3 次无答案超时），说明该差别由运行条件/延迟主导，两组都不支持优劣结论。
  - bovine 整题 A vs D（各 n=2）：A 的 marker 节点 0/2、D 2/2；**样本太小**，且 D 两次是同一内容答案，不能排除模型波动，也不能据此说科学管线有效。
  - `glm-5.3-flashx` 余额已耗尽（429/1113，expD24/25 全节点失败）；**在充值前无法产生新的真机证据**，只能做离线分析/代码与测试工作。
  - 基因 ID 命名空间不一致（Ensembl `ENSG…` vs HGNC/UniProt）会让交集算成 0（expD16 n4，k=1）；这是模型内容/数据映射行为，**未修**，也不属于 schema/交付通道。
  - `gene_set` 的本地 dev 仍是精确集合相等；expD16 n5 的 `content_diagnostics` 显示 3/3 命中 + 1 个多余基因，**官方对多余基因是否扣分未知**。
  - 「科学纠偏在**其它题目**上同样有效」——无法验证：其余公开题的统计口径是 BH / Bonferroni（与 padj 探针方向相反），且产物是数值/记录型，漂移在文本证据里不可见（#42）。expR31–33 只能跨题验证交付通道（菜单外选项、多选越界）。
  - node1/node3 多次出现「只在 `<final>` 宣称写文件、实际未调用 `write_file`」；恢复链能兜住，但模型 tool-use 不稳定本身未修复（属模型能力，不改官方 prompt）。
  - 官方 `MultipleChoiceGrader` 对「非选项内的缩写」如何计分（dev 打分判 wrong，官方 `_grading` 包缺失，未核对）。
  - 「valid 即正确」不成立：expR6 已给出反例。

## 下一步（按优先级）

1. （已完成）#37/#38/#39/#40 修复后的受控注入复跑（expR27/28/29，D、k=5）：2/5 → 2/5 → **5/5**；expR29 每个 run 只用 1 次纠偏，说明收益来自模板而非额外额度。**expR34 复跑同配置再次 5/5**（合并 10/10，可复现，但仍单题单节点）。
2. （已完成）#36 的 V4 判据：离线验证通过；expR28 中「用表格 `pvalue` 全表筛选」的 run 已被 `output_labels`/dev 判为错误，`C001` 不再假 PASS（该 run 的 `C001` 为 PASS 但被标签判据拦下，属正确的分工）。
3. 已在本轮收尾：#30 负结果、#33 受控注入证据、#34/#35 漏报与提示补强、#36 重算证据、#37 纠偏提前终止、#38 纠偏缺路径、#39 分组标签漂移、#40 schema+标签模板、#41 多选越界、#42 数值答案边界、#43 回复内答案回贴、官方计分包不可得结论、总览表更新到 #43。
4. （已完成）反向验证（expR30，无注入 A vs D，node2+node3，k=5）：A 10/10、D 8/10，**没有任何「正确被改坏」**；D 的两次失败均为模型侧（菜单内选错、多选越界），后者催生了 #41。D 成本约为 A 的 1.9–2.1×。
5. 下一步建议（未开工）：① （已完成本轮）把 `--recovery-attempts` 默认值从 2 提到 3（`experiment.py`/`smoke.py`；adapter 库层默认仍为 1）；② （已完成两次）用 #43 提示与 `--wrapup-after-tool-calls 8` 分别跑 node2/node3 自然条件（expR35/expR36）：**首轮漏写频率都没下降**（按节点：A 5→5→6、D 4→3→7），wrapup 阈值 8 只触发 1 次；结论是这一项属模型 tool-use 习惯，提示层收益有限，不建议再投资；③ 若要更强的结论，需要在**同口径**的另一题上复验（目前只有 bovine 是 unadjusted p；melanoma/kidney 均为 BH/Bonferroni，见 #42；本轮核查 adult_kidney 的 h5ad 数据在 HF 需授权（401），暂不可得）；④ 对 wall 接近预算的重节点（melanoma n6 约 300s/360s），可考虑把恢复预留从固定 30% 改为「至少够一次纠偏回合」（**评估后暂不做**：没有证据表明扩大预留能提高完成率，反而会挤压首轮；先改用 `timed_out` 标记收集可测数据）；⑤ （已完成）#44 的 Docker 后端可用后，可把 node2/node3 的自然条件对照（A vs D）整体搬到容器里重做，隔离「宿主 shim」这一变量；仍不能产出官方分数；⑥ （已完成本轮）#46：记录型答案（`differential_feature_set`）写成裸字符串的交付纠偏——expD11 复现缺陷（3/3 无纠偏）、expD12 修复后 3/3 形状修复；⑦ （已完成本轮）#47：数值答案写成字符串（`numeric_tolerance`）的交付纠偏——expD13 复现缺陷（3/3 无纠偏）、expD14 修复后 3/3 转数字，并按题目参数补了 tolerance-aware 的本地 dev（下界近似）；⑧ （已完成本轮）#48 边界确认：expD15（D 组）与 expD14（A 组）同型对比度错误各 1 次、正确率同为 2/3，科学管线无法验证分组定义，记录为已知边界；⑨ （已完成本轮）expD16 自然条件整题基线（melanoma 全 DAG、A、k=1）：n1 1.0、n2 内容按参数 3/3 匹配（dev 0 是精确判据所致）、n5 3/3+1 多余、n6 菜单内选错、n4 因 Ensembl↔HGNC 命名空间不一致答 0；已把 `content_diagnostics` 扩到 `gene_set`；⑩ （已完成本轮）整题 A vs D 扩到各 n=3（expD16+18 / expD17+19）：dev 均值 0.333 vs 0.400、wall 386s vs 479s；唯一明显差别是 n6（0/3 vs 2/3），不足以作有效性结论；⑪ （已完成本轮）n6 单点各 k=5（expD20/expD21）：A 3/5 vs D 1/5，方向与整题相反且 D 有 3 次无答案超时——**n6 单点受 360s 预算/延迟主导，不能用来判断 A/D**；下一步若要继续该问题，需把单点 wall 提到 ≥480s 或只做整题条件；⑫ （已完成本轮）bovine 整题自然条件 A/D 各 k=2（expD22/23）：A marker 0/2、D 2/2（node2/node3 两边全对）——n=2，不能作优劣结论；⑬ （已完成本轮）expD24/25 扩 n 时余额耗尽（flashx 也 429/1113），两轮作废；**真实运行在充值前无法继续**。
4. （已完成）node2/node3 的 A vs D 各 k=5 对照：正确率相同，D 组无 blocking → 属空转对照。
5. （已完成）#21：dev 明细区分 `wrong` / `wrong_off_menu`；prompt 仍与官方 renderer 一致，不改。
6. （已完成）#29：科学约束闸门已接入 pico 主循环（默认开启、缺失型证据不误杀、消融路径显式关闭）。
7. （已完成本轮）#7–#29 已修：schema 恢复 diff/结构模板、token 回填、跨节点 scope、异常隔离、deadline、瞬态重试、run 关闭、严格解析、报告诚实性、dev 明细、命令超时夹取、增量落盘、菜单外选项重写、脚本路径翻译、漂移判定收窄、恢复预算预留、核心闸门迁移。