"""UpgradeLens Streamlit 演示。

一个最小的本地可视化闭环：输入仓库/依赖/目标版本，复用核心 assess 流程
（代码扫描 -> 证据打包 -> 模型评估 -> 校验 -> patch 草稿），在页面上展示
结论、风险明细、证据与报告。

运行（需先安装 streamlit）：

    uv run --extra demo streamlit run demo/app.py

默认模型模式为 ``fake``，完全离线、无需任何 API Key 即可体验全流程。
切换到 ``live`` 模式才需要填写模型名 / API Key / Base URL。

注意：fake 模式并非真的"无文档"。为了让离线演示也能走通完整链路，
``demo/pipeline.py`` 会注入一条"例证性的官方文档引用"（``doc_chunk``，指向该
skill 真实的官方 ``source_id``），因此风险可被 verifier 提升为 VERIFIED，并能在
真实源码行上产出机械 patch。这一切都明确标注为 illustrative，并非真实模型推理，
也不代表真实文档检索质量。
"""

from __future__ import annotations

import streamlit as st

from demo.pipeline import run_assess
from upgradelens.report import render_markdown


def _render_overview(result: dict[str, object]) -> None:
    verified_report = result["verified"]  # type: ignore[assignment]
    skill = result["skill"]
    bundle = result["bundle"]

    cols = st.columns(4)
    cols[0].metric("结论", str(getattr(verified_report, "conclusion", "")))
    cols[1].metric("代码证据", str(len(getattr(bundle, "items", []))))
    verified_risks = getattr(verified_report, "verified_risks", [])
    degraded = getattr(verified_report, "degraded_risks", [])
    cols[2].metric("已验证风险", str(len(verified_risks)))
    cols[3].metric("降级风险", str(len(degraded)))

    skill_label = (
        f"{skill.skill_id} v{skill.version}" if skill is not None else "通用依赖（无专用 skill）"
    )
    st.caption(f"依赖：{result['code_report'].dependency_name}  解析 skill：{skill_label}")

    degradations = getattr(verified_report, "degradations", [])
    if degradations:
        with st.expander("评估降级说明", expanded=True):
            for note in degradations:
                st.warning(note)


def _render_risks(result: dict[str, object]) -> None:
    verified_report = result["verified"]  # type: ignore[assignment]
    rows = []
    for risk in getattr(verified_report, "verified_risks", []):
        rows.append(
            {
                "risk_id": getattr(risk, "risk_id", ""),
                "title": getattr(risk, "title", ""),
                "status": str(getattr(risk, "status", "")),
                "severity": str(getattr(risk, "severity", "")),
                "evidence": ", ".join(getattr(risk, "code_evidence_ids", [])),
            }
        )
    for risk in getattr(verified_report, "degraded_risks", []):
        rows.append(
            {
                "risk_id": getattr(risk, "risk_id", ""),
                "title": getattr(risk, "title", ""),
                "status": f"DEGRADED({getattr(risk, 'reason', '')})",
                "severity": str(getattr(risk, "severity", "")),
                "evidence": ", ".join(getattr(risk, "code_evidence_ids", [])),
            }
        )
    if rows:
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("没有可展示的风险条目。")


def _render_evidence(result: dict[str, object]) -> None:
    bundle = result["bundle"]
    items = getattr(bundle, "items", [])
    if not items:
        st.info("未发现该依赖的代码用法证据。")
        return
    rows = []
    for item in items:
        meta = getattr(item, "meta", {}) or {}
        rows.append(
            {
                "id": getattr(item, "evidence_id", ""),
                "kind": getattr(item, "kind", ""),
                "path": meta.get("path", ""),
                "line": meta.get("line", ""),
                "symbol": meta.get("symbol", ""),
            }
        )
    st.dataframe(rows, use_container_width=True)


def _render_report(result: dict[str, object]) -> None:
    verified_report = result["verified"]  # type: ignore[assignment]
    md = render_markdown(verified_report)
    st.markdown(md, unsafe_allow_html=False)


def _render_patch(result: dict[str, object]) -> None:
    draft = result.get("draft")
    if draft is None:
        st.info("该依赖未解析到可生成 patch 的专用 skill（或 skill 不允许生成）。")
        return
    diff = draft.to_unified_diff()
    if not diff:
        st.info(
            "未生成 patch 草稿。patch 生成器仅对 VERIFIED 风险、且 skill 提供"
            "安全机械改写规则的位置生效；语义改写类风险会被如实留空交人工复核。"
        )
        notes = getattr(draft, "notes", [])
        for note in notes:
            st.caption(note)
        return
    st.code(diff, language="diff")
    st.caption(f"应用规则：{', '.join(getattr(draft, 'applied_rules', []))}")


def main() -> None:
    st.set_page_config(page_title="UpgradeLens Demo", layout="wide")
    st.title("UpgradeLens — 依赖升级影响分析演示")

    with st.sidebar:
        st.header("分析输入")
        repo = st.text_input("仓库路径", value="tests/fixtures/eval/validator_direct_hit/repo")
        dependency = st.text_input("依赖名", value="pydantic")
        target_version = st.text_input("目标版本（具体版本号）", value="2.0.0")
        mode = st.selectbox("模型模式", ["fake", "replay", "live"], index=0)
        model = st.text_input("模型名（仅 live）", value="qwen-plus")
        api_key = st.text_input("API Key（仅 live）", type="password", value="")
        base_url = st.text_input("Base URL（仅 live）", value="")
        allow_quality_patch = st.checkbox("允许 quality patch（草稿可含需复核项）", value=True)
        run = st.button("运行评估", type="primary")

    if not run:
        st.info(
            "在左侧填写仓库与依赖后点击「运行评估」。默认使用 fake 模式（离线、无需 Key），"
            "已预填项目自带的 pydantic 示例仓库，可直接体验。"
        )
        return

    try:
        with st.spinner("评估中…"):
            result = run_assess(
                repo=repo,
                dependency=dependency,
                target_version=target_version,
                mode=mode,
                model=model,
                api_key=api_key,
                base_url=base_url,
                allow_quality_patch=allow_quality_patch,
            )
    except Exception as exc:  # surface any pipeline error in the UI
        st.exception(exc)
        return

    tab_overview, tab_risks, tab_evidence, tab_report, tab_patch = st.tabs(
        ["概览", "风险明细", "代码证据", "报告 Markdown", "Patch 草稿"]
    )
    with tab_overview:
        _render_overview(result)
    with tab_risks:
        _render_risks(result)
    with tab_evidence:
        _render_evidence(result)
    with tab_report:
        _render_report(result)
    with tab_patch:
        _render_patch(result)


if __name__ == "__main__":
    main()
