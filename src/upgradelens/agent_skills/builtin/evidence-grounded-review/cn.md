---
skill_id: evidence-grounded-review
name: 证据驱动审查
applies_to:
  - pr_review
  - issue_repair
  - security_review
  - breaking_change
  - dependency_upgrade
language: zh
version: "1.0.0"
description: >
  智能体输出的每一个结论都必须可追溯到具体证据。本技能剔除无证据支撑的断言，
  并为每条 findings 强制建立可验证的证据链。
when_to_use:
  - 产出 findings、风险评级或“这处会破坏”之类的断言时。
  - 总结代码改动、依赖差异或安全报告时。
steps:
  - 收集原始信号（diff 片段、检索到的文档、工具输出）。
  - 对每条候选 finding，记录其依赖的确切证据 id。
  - 丢弃没有任何 evidence_id 的 finding，或将其降级为“疑似”。
  - 依据证据强度（而非直觉）为每条 finding 标注 confidence。
  - 在总结中引用证据 id；绝不陈述追溯链无法到达的事实。
constraints:
  - 状态为 “verified” 的 finding 必须至少携带一个 evidence_id。
  - 不得编造追溯链中不存在的文件路径、行号或 API 名称。
  - 显式区分 “verified / suspected / informational” 三个层级。
completion_criteria:
  - 每条产出的 finding 都带有 evidence_id，或被显式标记为 “疑似/信息”。
  - 总结只引用有追溯链支撑的 finding。
evidence_policy:
  required_for_verified: true
  tiers: [verified, suspected, informational]
---

# 证据驱动审查

升级、审查与修复之所以失败，往往是因为智能体断言了它无法证明的东西。本技能
把“证据链”作为信任的基本单位。

## 原则

1. **无证据，无断言。** 若你无法指向具体的 diff 行、文档片段或工具输出，该陈述
   就不是一条 finding，而是一个假设，必须如实标注。
2. **三层而非一层。** `verified`（证据充分且强）、`suspected`（合理但证据弱/不全）、
   `informational`（上下文，无需动作）。验证器会拒绝没有 `evidence_id` 的
   `verified` finding。
3. **置信度来自证据，而非感觉。** 置信度由证据的直接性与权威性决定，而非智能体
   “觉得有多确定”。

## 如何应用

- 收集信号时，给每条加 id：`diff:src/x.py:42`、`doc:pydantic-migration:validator`、
  `tool:grep:Config`。
- 写 finding 时设置 `evidence_ids: [...]` 并选择层级。
- 最终总结中引用这些 id，读者应能仅凭追溯链重建推理过程。
