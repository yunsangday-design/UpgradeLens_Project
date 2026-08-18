---
skill_id: pydantic_v1_to_v2
name: Pydantic v1 → v2 迁移
package_names:
  - pydantic
source_version_spec: ">=1,<2"
target_version_spec: ">=2,<3"
priority: 100
support_status: dedicated
risk_categories:
  - validator
  - root_validator
  - config
  - orm_mode
  - serialization
  - parsing
allow_patch_draft: true
version: "1.0.0"
---

# Pydantic v1 → v2 迁移

针对 pydantic 从 1.x 升级到 2.x 的专用知识包，覆盖已弃用的 validator/root_validator、
Config/orm_mode 配置、`.dict()`/`.json()`/`parse_obj()` 等用法，并给出检索官方迁移指南的模式。

## Limitations

仅覆盖计划 9.7 列出的第一批模式；跨文件数据流、别名传播与 import * 不在本阶段范围内。
