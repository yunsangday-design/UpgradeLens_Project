---
skill_id: verification-before-completion
name: 完成前验证
applies_to:
  - pr_review
  - issue_repair
  - security_review
  - breaking_change
  - dependency_upgrade
language: zh
version: "1.0.0"
description: >
  智能体只有在独立验证器运行通过后，才能宣称成功。本技能禁止在没有验证结果支撑时
  使用 “fixed / passed / safe” 之类的措辞。
when_to_use:
  - 在标记任务完成、finding 已解决或补丁就绪之前。
steps:
  - 枚举本任务必须通过的检查（测试、静态分析、schema）。
  - 运行验证器，捕获结构化的 VerificationResult。
  - 将每条声称已修复的内容映射到确认它的检查。
  - 若任一必需检查失败，保持 “failed / needs_human” 状态，不得重新贴标签。
constraints:
  - 在没有通过验证的情况下，绝不使用 “fixed / passed / resolved / safe”。
  - 不得自我证明成功；验证器才是权威。
  - 验证失败时必须下调声称，而非轻描淡写地略过。
completion_criteria:
  - 每条正向声称在 VerificationResult 中都有对应通过的检查。
  - 总结如实引用验证结果中的验证状态。
evidence_policy:
  verification_required: true
---

# 完成前验证

置信不是结果。本技能让验证器成为“完成”的守门人。

## 规则

1. **没有运行就没有结论。** 生成或引用一个 `VerificationResult`；没有它的声称
   一律不予认可。
2. **声称映射到检查。** 每条“这已修复”都必须指向证明它的具体检查。游离的声称
   会被拒绝。
3. **失败要响亮。** 红色检查保持红色，不要在验证器说不通时把总结改写成“已解决”。

## 措辞护栏

- 允许：“验证器报告 12/12 检查通过”、“补丁解决了 finding F3（由测试 T2 确认）”。
- 禁止：“我修好了”、“现在应该安全了”、“大概能跑”。
