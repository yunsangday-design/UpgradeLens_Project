---
skill_id: sqlalchemy_v1_to_v2
name: SQLAlchemy 1.x -> 2.0
package_names:
  - sqlalchemy
source_version_spec: ">=1.4,<2"
target_version_spec: ">=2,<3"
priority: 100
support_status: dedicated
risk_categories:
  - declarative_import
  - hybrid_import
  - session_query
  - query_get
  - engine_execute
allow_patch_draft: true
version: "1.0.0"
---

# SQLAlchemy 1.x -> 2.0

针对 SQLAlchemy 从 1.x 升级到 2.0 的专用知识包。2.0 统一了 1.x 与 2.0 两套
API，最常见的破坏性变更包括：(a) sqlalchemy.ext.declarative 包被移除，declarative_base
等需改从 sqlalchemy.orm 导入；(b) session.query()/Query.get() 被弃用，需改写为
select() + execute() 的 2.0 风格；(c) engine.execute() 的执行模型与参数风格变更。
本包对可机械、向后兼容的 import 迁移提供自动 patch；query/execute 的语义改写仅作为
风险呈现，交人工复核。

## Limitations

仅覆盖第一批可被静态识别的模式。session.query() -> select() 的跨行语义改写无法安全
机械完成，不在自动 patch 范围；外键 ondelete 行为、relationship lazy 默认、事件监听等
行为变更不在本阶段范围内。
