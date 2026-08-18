---
skill_id: safe-dependency-migration
name: 安全依赖迁移
applies_to:
  - dependency_upgrade
language: zh
version: "1.0.0"
description: >
  适用于“任何”依赖升级的与能力无关的方法。它刻意不包含任何版本相关事实——那些
  事实存放于共享 RAG 语料——而是规定一套安全、证据驱动的迁移工作流，以及机械改写
  的来源（TransformationPack）。
when_to_use:
  - 将某个依赖从一个版本区间迁移到另一个版本区间，跨任意生态。
steps:
  - 确认精确的 from/to 版本区间，以及代码中实际使用的公开 API 面。
  - 从 RAG 语料检索该依赖可信的迁移文档。
  - 枚举与“已使用 API 面”相交的破坏性变更。
  - 仅在已验证的风险点运行对应的 TransformationPack 改写。
  - 在宣称完成前，用项目的测试/静态检查验证每条改写。
constraints:
  - 不得在本技能中硬编码版本事实、已移除的 API 或包名。
  - 机械改写只来自 TransformationPack，不得在此处自行编造。
  - 仅在证据表明旧 API 确实被使用时，才提出改写。
  - 每条破坏性变更 finding 必须引用文档来源与使用证据。
completion_criteria:
  - from/to 区间与已使用 API 面均有证据陈述。
  - 破坏性变更被限定在实际使用的 API，而非整份变更日志。
  - 改写经过证据门控，并在称为“完成”前通过验证。
evidence_policy:
  required_for_breaking_change: true
  facts_belong_to_corpus: true
---

# 安全依赖迁移

本技能刻意**不是**某个库的知识包。它是一套适用于任何升级的方法，把“如何迁移”
（此处）与“改了什么”（RAG 语料）以及“如何机械改写”（TransformationPack）分离开。

## 方法

1. **界定迁移范围。** 陈述 `from` 与 `to` 版本区间，以及代码实际触及的 API 面。
   臆测 API 面既浪费精力又制造虚假风险。
2. **从语料取事实。** 共享 RAG 语料保存可信的迁移文档。检索，而非记忆——这正是
   本技能不带版本特定知识的原因。
3. **与使用情况取交集。** 破坏性变更只有在代码使用了受影响 API 时才有意义。让
   变更日志与代码符号扫描相交。
4. **机械改写并验证。** 仅应用匹配“已验证使用点”的 TransformationPack 规则。手写
   改写只是提案，不是事实。
5. **完成前验证。** 验证器为“已迁移”把关。参见 `verification-before-completion`。

## 本技能不是什么

- 不是 pydantic/sqlalchemy/任何库的速查表。
- 不是粘贴已删除函数名或版本号的地方。
- 不是补丁生成器；机械改写归 TransformationPack 所有。
