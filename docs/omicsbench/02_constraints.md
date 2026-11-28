# 02 · Science Constraint 管线（P1/P2/P3）

## 结构

`goaltree/scientific/`：`contracts.py`（可序列化契约对象）、`compiler.py`（约束抽取）、
`evidence.py`（证据原语）、`verifier.py`（证据校验）、`recovery.py`（纠偏闭环）。
默认关闭，仅显式引入 `goaltree.scientific` 才生效；普通 Coding Agent 行为不变。

## P1 · Constraint Compiler（最小集）

确定性正则抽取：Bonferroni / BH(FDR) / unadjusted 校正方法；adjusted-p / unadjusted-p /
logFC / Top-N 阈值；up/down 方向；样本/基因范围。每项含 `source_span`、`confidence=explicit`、
`verification_strategy`。解析失败的项进 `unresolved_requirements`，**不**静默当通过，**不**把缺省值当用户要求。

已测（`tests/test_omicsbench_scientific.py`）：Bonferroni 保留不默认 BH；adjusted-p/logFC/Top-N
正确抽取；省略阈值不伪造；同一需求改动产生不同 `source_prompt_hash`。

## P2 · Evidence Verifier

- V1 Criteria Drift：契约 method / p / logFC 约束 vs 实际执行命令脚本文本；`_ADJUST_TERMS`
  （padj/BH/FDR/Bonferroni/qvalue…）探测方法替换。区分 PASS / VIOLATION / UNVERIFIED /
  NOT_APPLICABLE。
- V2 Scope Drift：只认可检证的样本/特征使用清单；最终表小**不**等于漏分析全量，缺证据记 UNVERIFIED。
- V3 Unsupported Answer：答案是否可溯源到产物；空答案 = VIOLATION，缺 provenance = UNVERIFIED。

负向测试已覆盖：正确分析不被误拦、方法声称正确但真实中间结果不符可识别、空文件不因存在即通过、
轨迹缺失记 UNVERIFIED 不编造。

## P3 · Targeted Recovery

仅当 `VIOLATION` 且 `severity=blocking` 才生成定向纠偏消息（Requirement/Observed/Evidence），
固定 `max_attempts` 上限，`attempt >= max_attempts` 即停止。不读答案、不读 grader 标签、不扩大工具权限。