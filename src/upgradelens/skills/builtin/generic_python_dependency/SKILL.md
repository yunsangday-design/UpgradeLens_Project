---
skill_id: generic_python_dependency
name: 通用 Python 依赖升级
package_names:
  - "*"
source_version_spec: null
target_version_spec: null
priority: 0
support_status: generic
risk_categories: []
allow_patch_draft: false
version: "1.0.0"
---

# 通用 Python 依赖升级

通用兜底知识包：当没有专门的依赖知识包时启用。仅做基础静态证据收集，
不声明任何特定破坏性变更知识，结论需人工确认。

## Limitations

不覆盖任何特定依赖的破坏性变更；检索与风险判断能力被显式降级（capability note
随选择结果一同返回，供下游报告标注降级）。
