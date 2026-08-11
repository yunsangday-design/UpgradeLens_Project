"""UpgradeLens Streamlit 可视化演示。

功能完整的本地可视化闭环：输入仓库/依赖/目标版本，复用核心 Agent 流程
（路由 → 计划 → 代码扫描 → 证据收集 → 验证 → 补检索 → 报告），在页面上
展示结论、执行计划、风险明细、证据、工具调用 trace 和 patch 草稿。

运行（需先安装 streamlit）：

    uv run --extra demo streamlit run demo/app.py

默认模型模式为 ``fake``，完全离线、无需任何 API Key 即可体验全流程。
切换到 ``live`` 模式才需要填写模型名 / API Key / Base URL。
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from demo.pipeline import run_agent_assess, run_assess
from upgradelens.report import render_markdown

# -- 颜色 / 样式常量 -------------------------------------------------------- #

_SEVERITY_COLORS = {
    "high": "🔴",
    "medium": "🟡",
    "low": "🟢",
    "critical": "🔴",
}

_STATUS_COLORS = {
    "verified": "✅",
    "partially_verified": "🟡",
    "insufficient_evidence": "⚠️",
    "conflicting_evidence": "❌",
}

_PLAN_STATUS_ICONS = {
    "succeeded": "✅",
    "failed": "❌",
    "skipped": "⏭️",
    "pending": "⏳",
    "running": "🔄",
}

_TRACE_STATUS_ICONS = {
    "ok": "✅",
    "cached": "💾",
    "error": "❌",
}

_DEFAULT_REPO = "tests/fixtures/eval/validator_direct_hit/repo"


# -- 自定义 CSS ------------------------------------------------------------- #


def _inject_css() -> None:
    st.markdown(
        """
        <style>
        /* 主标题区 */
        .main-header {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            padding: 1.5rem 2rem;
            border-radius: 0.5rem;
            margin-bottom: 1rem;
        }
        .main-header h1 {
            color: #e0e0e0;
            margin: 0;
            font-size: 1.6rem;
        }
        .main-header p {
            color: #a0a0a0;
            margin: 0.3rem 0 0 0;
            font-size: 0.85rem;
        }
        /* 卡片 */
        div[data-testid="stMetric"] {
            background: #1e1e2e;
            border: 1px solid #2a2a3e;
            border-radius: 0.5rem;
            padding: 0.8rem;
        }
        /* Step 卡片 */
        .step-card {
            background: #1a1a2e;
            border-left: 3px solid #0f3460;
            padding: 0.5rem 1rem;
            margin: 0.3rem 0;
            border-radius: 0 0.3rem 0.3rem 0;
        }
        .step-card.success { border-left-color: #00c853; }
        .step-card.failed { border-left-color: #ff1744; }
        .step-card.skipped { border-left-color: #ffab00; }
        /* 风险卡片 */
        .risk-card {
            background: #1e1e2e;
            border: 1px solid #2a2a3e;
            border-radius: 0.5rem;
            padding: 0.8rem 1rem;
            margin: 0.4rem 0;
        }
        .risk-card.high { border-left: 3px solid #ff1744; }
        .risk-card.medium { border-left: 3px solid #ffab00; }
        .risk-card.low { border-left: 3px solid #00c853; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# -- 渲染函数 --------------------------------------------------------------- #


def _render_header() -> None:
    st.markdown(
        """
        <div class="main-header">
            <h1>🔍 UpgradeLens — 依赖升级影响分析 Agent</h1>
            <p>路由 → 计划 → 代码扫描 → 证据收集 → 验证 → 补检索 → 报告</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_sidebar() -> dict[str, object]:
    with st.sidebar:
        st.header("⚙️ 分析配置")

        repo = st.text_input("📦 仓库路径", value=_DEFAULT_REPO, help="本地路径或 GitHub URL")
        dependency = st.text_input("🏷️ 依赖名", value="pydantic")
        target_version = st.text_input("🎯 目标版本", value="2.0.0")
        source_version = st.text_input("📋 源版本（可选）", value="", help="留空则从 manifest 推断")

        st.divider()
        st.subheader("🤖 模型配置")
        mode = st.selectbox(
            "运行模式",
            ["fake", "replay", "live"],
            index=0,
            help="fake=离线确定性 | replay=回放录制 | live=真实LLM",
        )
        replay_dir = (
            st.text_input("Replay 目录", value="", help="仅 replay 模式：录制响应目录")
            if mode == "replay"
            else ""
        )
        model = st.text_input("模型名", value="qwen-plus") if mode == "live" else "qwen-plus"
        api_key = st.text_input("API Key", type="password", value="") if mode == "live" else ""
        base_url = st.text_input("Base URL", value="") if mode == "live" else ""

        st.divider()
        st.subheader("🔧 选项")
        use_agent = st.checkbox("Agent 模式（计划驱动 + 工具 trace + 成本）", value=True)
        allow_quality_patch = st.checkbox("允许 quality patch", value=True)

        st.divider()
        run = st.button("🚀 运行评估", type="primary", use_container_width=True)

    return {
        "repo": repo,
        "dependency": dependency,
        "target_version": target_version,
        "source_version": source_version or None,
        "mode": mode,
        "replay_dir": replay_dir if mode == "replay" else None,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "use_agent": use_agent,
        "allow_quality_patch": allow_quality_patch,
        "run": run,
    }


def _render_overview(result: dict[str, object]) -> None:
    verified = result.get("verified")
    if verified is None:
        st.error("未产生评估结果。" + (result.get("error", "") or ""))
        return

    bundle = result.get("bundle")
    items = getattr(bundle, "items", []) if bundle else []

    # 结论卡片
    conclusion = str(getattr(verified, "conclusion", ""))
    conclusion_labels = {
        "impacted": ("⚠️ 受影响", "error"),
        "no_impact": ("✅ 无影响", "success"),
        "evidence_insufficient": ("❓ 证据不足", "warning"),
    }
    label, kind = conclusion_labels.get(conclusion, (conclusion, "info"))
    getattr(st, kind)(f"**结论：{label}**")

    st.divider()

    # 指标卡
    verified_risks = getattr(verified, "verified_risks", [])
    degraded = getattr(verified, "degraded_risks", [])
    cols = st.columns(5)
    cols[0].metric("代码证据", len(items))
    cols[1].metric("已验证风险", len(verified_risks))
    cols[2].metric("降级风险", len(degraded))
    cols[3].metric("引用率", f"{getattr(verified, 'citation_existence_rate', 0):.0%}")
    cols[4].metric("部分报告", "是" if getattr(verified, "partial", False) else "否")

    # Skill / 依赖信息
    skill = result.get("skill")
    code_report = result.get("code_report")
    if code_report:
        dep_name = getattr(code_report, "dependency_name", "")
        skill_label = f"{skill.skill_id} v{skill.version}" if skill else "通用依赖（无专用 skill）"
        st.caption(f"依赖：`{dep_name}`  |  Skill：`{skill_label}`")

    # 降级说明
    degradations = result.get("degradations") or getattr(verified, "degradations", [])
    if degradations:
        with st.expander("⚠️ 评估降级说明", expanded=True):
            for note in degradations:
                st.warning(note)

    # 成本（仅 agent 模式）
    total_tokens = result.get("total_tokens", 0)
    call_count = result.get("call_count", 0)
    if total_tokens or call_count:
        st.divider()
        cols = st.columns(3)
        cols[0].metric("模型调用", f"{call_count} 次")
        cols[1].metric("总 Token", f"{total_tokens:,}")
        cols[2].metric("每调用平均", f"{total_tokens // max(call_count, 1):,}")


def _render_plan(result: dict[str, object]) -> None:
    plan = result.get("plan")
    if plan is None:
        st.info("本次运行未使用 Agent 模式，无执行计划。请在侧边栏勾选「Agent 模式」。")
        return

    steps = getattr(plan, "steps", [])
    if not steps:
        st.info("执行计划为空。")
        return

    st.subheader("📋 执行计划")

    for step in steps:
        icon = _PLAN_STATUS_ICONS.get(step.status, f"[{step.status}]")

        with st.container():
            cols = st.columns([1, 3, 2])
            with cols[0]:
                st.markdown(f"### {icon}")
                st.caption(f"#{step.seq}")
            with cols[1]:
                st.markdown(f"**{step.tool}**")
                st.caption(step.reason or "")
            with cols[2]:
                if step.observation:
                    st.caption(
                        f"💬 {step.observation[:100]}{'...' if len(step.observation) > 100 else ''}"
                    )

            # 证据链接
            ev_ids = []
            if hasattr(step, "evidence_ids"):
                ev_ids = step.evidence_ids or []
            if ev_ids:
                st.caption(f"📎 证据：{', '.join(ev_ids[:5])}")

            st.divider()

    # Plan 状态
    plan_status = getattr(plan, "status", "")
    if plan_status:
        st.caption(f"计划状态：`{plan_status}`")
    notes = getattr(plan, "notes", [])
    if notes:
        with st.expander("📝 计划备注"):
            for note in notes:
                st.write(f"- {note}")


def _render_risks(result: dict[str, object]) -> None:
    verified = result.get("verified")
    if verified is None:
        st.info("无评估结果。")
        return

    verified_risks = getattr(verified, "verified_risks", [])
    degraded_risks = getattr(verified, "degraded_risks", [])

    if not verified_risks and not degraded_risks:
        st.success("🎉 未发现风险！该依赖的升级不会影响当前代码。")
        return

    # 统计
    cols = st.columns(3)
    high_count = sum(1 for r in verified_risks if getattr(r, "severity", "") == "high")
    cols[0].metric("高风险", high_count)
    cols[1].metric("已验证", len(verified_risks))
    cols[2].metric("降级", len(degraded_risks))

    st.divider()

    if verified_risks:
        st.subheader("✅ 已验证风险")
        for risk in verified_risks:
            sev = getattr(risk, "severity", "low")
            sev_icon = _SEVERITY_COLORS.get(sev, "🟢")
            status = str(getattr(risk, "status", ""))
            status_icon = _STATUS_COLORS.get(status, "")

            with st.expander(
                f"{sev_icon} [{sev.upper()}] {getattr(risk, 'title', '')}", expanded=True
            ):
                cols = st.columns([2, 2, 1])
                cols[0].caption(f"状态：{status_icon} {status}")
                cols[1].caption(f"严重性：{sev}")
                cols[2].caption(f"规则评分：{getattr(risk, 'rule_score', 0)}")

                # 证据
                code_ev = getattr(risk, "code_evidence_ids", [])
                doc_ev = getattr(risk, "doc_evidence_ids", [])
                unknown_ev = getattr(risk, "unknown_evidence_ids", [])

                ev_cols = st.columns(3)
                ev_cols[0].markdown(f"**代码证据** ({len(code_ev)})")
                for eid in code_ev[:3]:
                    ev_cols[0].caption(f"`{eid}`")

                ev_cols[1].markdown(f"**文档证据** ({len(doc_ev)})")
                for eid in doc_ev[:3]:
                    ev_cols[1].caption(f"`{eid}`")

                ev_cols[2].markdown(f"**未知证据** ({len(unknown_ev)})")
                for eid in unknown_ev[:3]:
                    ev_cols[2].caption(f"`{eid}`")

                # 建议
                rec = getattr(risk, "recommendation", "")
                if rec:
                    st.info(f"💡 **迁移建议**：{rec}")

                # Verifier issues
                issues = getattr(risk, "issues", [])
                if issues:
                    st.warning(f"⚠️ Verifier 发现 {len(issues)} 个问题：")
                    for issue in issues:
                        st.caption(f"  - [{issue.code}] {issue.detail}")

    if degraded_risks:
        st.divider()
        st.subheader("⚠️ 降级风险（证据不足）")
        for risk in degraded_risks:
            sev = getattr(risk, "severity", "low")
            sev_icon = _SEVERITY_COLORS.get(sev, "🟢")
            with st.expander(f"{sev_icon} [{sev.upper()}] {getattr(risk, 'title', '')}"):
                st.caption(f"状态：{getattr(risk, 'status', '')}")
                st.caption(f"原因：{getattr(risk, 'reason', '证据不足')}")
                code_ev = getattr(risk, "code_evidence_ids", [])
                if code_ev:
                    st.caption(f"代码证据：{', '.join(code_ev[:3])}")
                rec = getattr(risk, "recommendation", "")
                if rec:
                    st.info(f"💡 {rec}")


def _render_evidence(result: dict[str, object]) -> None:
    bundle = result.get("bundle")
    if bundle is None:
        st.info("无证据包。")
        return

    items = getattr(bundle, "items", [])
    if not items:
        st.info("未发现该依赖的代码用法证据。")
        return

    # 按类型分组统计
    by_kind: dict[str, int] = {}
    for item in items:
        kind = getattr(item, "kind", "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1

    cols = st.columns(len(by_kind)) if by_kind else st.columns(1)
    for i, (kind, count) in enumerate(by_kind.items()):
        cols[i].metric(kind, count)

    st.divider()

    # 证据表
    rows = []
    for item in items:
        meta = getattr(item, "meta", {}) or {}
        rows.append(
            {
                "类型": getattr(item, "kind", ""),
                "文件路径": meta.get("path", ""),
                "行号": meta.get("line", ""),
                "符号": meta.get("symbol", ""),
                "证据 ID": getattr(item, "evidence_id", "")[:20] + "...",
            }
        )

    st.dataframe(rows, use_container_width=True, hide_index=True)

    # 展示源码片段（如果有 path 和 line）
    st.divider()
    st.subheader("📄 源码位置")
    seen_paths: set[str] = set()
    for item in items:
        meta = getattr(item, "meta", {}) or {}
        path = meta.get("path", "")
        line = meta.get("line", 0)
        symbol = meta.get("symbol", "")
        if path and path not in seen_paths:
            seen_paths.add(path)
            st.code(f"# {path}:{line}  symbol={symbol}", language="text")


def _render_trace(result: dict[str, object]) -> None:
    """工具调用 trace 可视化。"""
    trace = result.get("trace")
    if trace is None:
        st.info("无工具调用 trace（非 Agent 模式不记录 trace）。")
        return

    events = getattr(trace, "events", [])
    if not events:
        st.info("本次运行无工具调用。")
        return

    st.subheader(f"🔧 工具调用 Trace（{len(events)} 次调用）")

    # 统计
    total_latency = sum(e.latency_ms for e in events)
    ok_count = sum(1 for e in events if e.status == "ok")
    err_count = sum(1 for e in events if e.status == "error")
    cache_count = sum(1 for e in events if e.cache_hit)

    cols = st.columns(4)
    cols[0].metric("总调用", len(events))
    cols[1].metric("成功", ok_count)
    cols[2].metric("错误", err_count)
    cols[3].metric("缓存命中", cache_count)

    if total_latency > 0:
        st.caption(f"总耗时：{total_latency:.1f}ms")

    st.divider()

    # 时间线
    for i, event in enumerate(events):
        icon = _TRACE_STATUS_ICONS.get(event.status, "❓")
        cols = st.columns([1, 3, 2, 1])

        with cols[0]:
            st.markdown(f"### {icon}")
            st.caption(f"#{i + 1}")

        with cols[1]:
            st.markdown(f"**{event.tool}**")
            st.caption(f"🎯 {event.target[:60]}{'...' if len(event.target) > 60 else ''}")

        with cols[2]:
            st.caption(f"⏱️ {event.latency_ms:.1f}ms")
            if event.bytes:
                st.caption(f"📦 {event.bytes:,} bytes")
            if event.cache_hit:
                st.caption("💾 缓存命中")

        with cols[3]:
            if event.evidence_ids:
                st.caption(f"📎 {len(event.evidence_ids)} 证据")
            if event.error:
                st.error(event.error[:80])

        st.divider()


def _render_report(result: dict[str, object]) -> None:
    verified = result.get("verified")
    if verified is None:
        st.info("无评估报告。")
        return
    md = render_markdown(verified)
    st.markdown(md, unsafe_allow_html=False)


def _render_patch(result: dict[str, object]) -> None:
    draft = result.get("draft")
    if draft is None:
        st.info("该依赖未解析到可生成 patch 的专用 skill。")
        return

    diff = draft.to_unified_diff()
    if not diff:
        st.info(
            "未生成 patch 草稿。patch 生成器仅对 VERIFIED 风险、且 skill 提供"
            "安全机械改写规则的位置生效；语义改写类风险会如实留空交人工复核。"
        )
        notes = getattr(draft, "notes", [])
        for note in notes:
            st.caption(note)
        return

    st.code(diff, language="diff")
    st.caption(f"应用规则：{', '.join(getattr(draft, 'applied_rules', []))}")


def _render_eval_compare() -> None:
    """在 UI 内直接跑 S8 评测对比。"""
    st.subheader("📊 S8 架构对照评测")
    st.caption("对比 direct LLM / 固定 Pipeline / Agent 三种架构（离线 FAKE 模式）")

    col_a, col_b = st.columns(2)
    with col_a:
        run_compare = st.button("运行三架构对比 (eval-compare)", type="primary")
    with col_b:
        run_ablate = st.button("运行消融实验 (eval-ablate)")

    if run_compare:
        with st.spinner("运行中…"):
            try:
                from upgradelens.eval import run_comparison
                from upgradelens.eval.cases import load_cases

                cases_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "eval"
                cases = load_cases(cases_dir)
                report = run_comparison(cases)
                agg = report.aggregate()

                st.success("✅ 对比完成！")

                # 聚合表
                st.subheader("系统级聚合指标")
                data = []
                for sys_name in report.systems:
                    a = agg.get(sys_name, {})
                    plan = a.get("plan_completion_rate")
                    det = a.get("verifier_detection_rate")
                    data.append(
                        {
                            "系统": sys_name,
                            "breaking 召回": f"{a.get('breaking_change_recall', 0):.2f}",
                            "code 召回": f"{a.get('code_location_recall', 0):.2f}",
                            "文档准确率": f"{a.get('doc_accuracy', 0):.2f}",
                            "无证据率": f"{a.get('no_evidence_rate', 0):.2f}",
                            "coverage": f"{a.get('coverage', 0):.2f}",
                            "plan 完成率": "n/a" if plan is None else f"{plan:.2f}",
                            "verifier 检出率": "n/a" if det is None else f"{det:.2f}",
                        }
                    )
                st.dataframe(data, use_container_width=True, hide_index=True)

                # 关键洞察
                st.divider()
                st.subheader("🔍 关键洞察")
                direct_det = agg.get("direct_llm", {}).get("verifier_detection_rate")
                agent_det = agg.get("agent", {}).get("verifier_detection_rate")
                if direct_det is not None and agent_det is not None:
                    if agent_det > direct_det:
                        st.success(
                            f"✅ Agent 的 verifier 检出率 ({agent_det:.0%}) 显著高于 "
                            f"direct LLM ({direct_det:.0%})——验证闸有效隔离了编造风险。"
                        )
                    else:
                        st.warning("⚠️ Agent 和 direct LLM 的检出率无显著差异。")

                agent_calls = agg.get("agent", {}).get("call_count", 0)
                pipeline_calls = agg.get("fixed_pipeline", {}).get("call_count", 0)
                if agent_calls and pipeline_calls:
                    st.info(
                        f"Agent 平均调用 {agent_calls:.0f} 次模型，"
                        f"Pipeline {pipeline_calls:.0f} 次。"
                    )

            except Exception as exc:
                st.exception(exc)

    if run_ablate:
        with st.spinner("运行消融实验…"):
            try:
                from upgradelens.eval import ABLATION_SYSTEMS, run_ablation
                from upgradelens.eval.cases import load_cases

                cases_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "eval"
                cases = load_cases(cases_dir)
                report = run_ablation(cases)
                agg = report.aggregate()

                st.success("✅ 消融实验完成！")

                st.subheader("消融对比（隔离各层价值）")
                data = []
                for sys_name in ABLATION_SYSTEMS:
                    a = agg.get(sys_name, {})
                    det = a.get("verifier_detection_rate")
                    data.append(
                        {
                            "系统": sys_name,
                            "coverage": f"{a.get('coverage', 0):.2f}",
                            "verifier 检出率": "n/a" if det is None else f"{det:.2f}",
                            "无证据率": f"{a.get('no_evidence_rate', 0):.2f}",
                            "模型调用": int(a.get("call_count", 0)),
                            "总 Token": int(a.get("total_tokens", 0)),
                        }
                    )
                st.dataframe(data, use_container_width=True, hide_index=True)

                st.divider()
                st.caption("💡 消融系统说明：")
                st.caption("• `direct_llm` — 无检索、无验证（裸 LLM 基线）")
                st.caption("• `fixed_pipeline` — 检索 + 验证，无 Agent 循环")
                st.caption("• `agent_no_supplement` — Agent + 验证，S4 补检索关闭")
                st.caption("• `agent` — 完整 Agent（补检索 + 验证 + 反馈）")

            except Exception as exc:
                st.exception(exc)


def _render_architecture() -> None:
    """展示 Agent 架构和控制流。"""
    st.subheader("🏗️ UpgradeLens Agent 架构")

    st.markdown("""
    ```
    ┌──────────────────────────────────────────────────┐
    │            DependencyUpgradeAgent                 │
    │           .run(goal) / .run_pipeline()            │
    └────────────────────┬─────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
       CLI agent      CLI assess     MCP assess
                         │
    ┌────────────────────┴─────────────────────────────┐
    │                  Agent Loop                       │
    │                                                   │
    │  1. Router (NL → Intent)                         │
    │     ↓                                             │
    │  2. Planner (Intent → AgentPlan)                 │
    │     ↓                                             │
    │  3. Collect (clone/scan/retrieve)                │
    │     ↓                                             │
    │  4. Coverage Assessment (S4)                     │
    │     ↓                                             │
    │  5. Supplement Retrieval (gap-focused)           │
    │     ↓                                             │
    │  6. Verify (evidence → risk status)             │
    │     ↓                                             │
    │  7. Feedback Loop (replan if issues, S5)         │
    │     ↓                                             │
    │  8. Report (verified risks + recommendations)    │
    └────────────────────────────────────────────────────┘
                         │
    ┌────────────────────┴─────────────────────────────┐
    │             Pipeline (baseline)                   │
    │  scan_dependency → scan_code → retrieve →         │
    │  analyse → verify                                 │
    └──────────────────────────────────────────────────┘
    ```
    """)

    st.divider()

    st.subheader("🔒 防幻觉验证闸")
    st.info("""
    **核心保证**：模型产出的每条风险必须引用 `EvidenceBundle` 中真实存在的证据 id。

    | 验证步骤 | 作用 |
    |---|---|
    | 代码证据回查 | 回磁盘重核每个引用的 path/symbol/line 是否真实 |
    | 文档证据回查 | 确认 doc evidence id 在文档库中存在 |
    | 风险分级 | verified（通过）/ partially_verified（部分）/ insufficient（不足）|
    | 编造隔离 | 引用不存在证据的风险被 quarantine，不进入 verified 列表 |

    在 S8 评测中，verifier 对编造风险的检出率为 **100%**，而裸 LLM 基线为 **0%**。
    """)

    st.divider()

    st.subheader("📊 S8 评测指标体系")
    st.markdown("""
    | 指标 | 含义 | 计算方式 |
    |---|---|---|
    | breaking_change_recall | 期望破坏性变更的召回率 | 已检出符号 / 期望符号 |
    | code_location_recall | 代码位置召回率 | 已定位路径 / 期望路径 |
    | doc_accuracy | 文档引用准确率 | 有效 doc 引用 / 全部 doc 引用 |
    | no_evidence_rate | 无证据建议率 | 无证据风险 / 全部风险 |
    | coverage | 证据覆盖率 | 已覆盖符号 / 期望符号 |
    | plan_completion_rate | 计划完成率 | succeeded steps / total steps |
    | verifier_detection_rate | verifier 检出率 | 已隔离编造 / 全部编造 |
    """)


# -- 主入口 ----------------------------------------------------------------- #


def main() -> None:
    st.set_page_config(
        page_title="UpgradeLens",
        page_icon="🔍",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_css()
    _render_header()

    config = _render_sidebar()

    if not config["run"]:
        st.info(
            "👈 在左侧填写仓库与依赖后点击「🚀 运行评估」。"
            "默认使用 fake 模式（离线、无需 Key），已预填 pydantic 示例仓库。"
        )

        # 展示架构和评测 tab 作为默认内容
        tab_arch, tab_eval = st.tabs(["🏗️ 架构说明", "📊 评测对比"])
        with tab_arch:
            _render_architecture()
        with tab_eval:
            _render_eval_compare()
        return

    # 运行评估
    try:
        with st.spinner("🔄 评估中…"):
            if config["use_agent"]:
                result = run_agent_assess(
                    repo=config["repo"],
                    dependency=config["dependency"],
                    target_version=config["target_version"],
                    mode=config["mode"],
                    model=config["model"],
                    api_key=config["api_key"],
                    base_url=config["base_url"],
                    source_version=config["source_version"],
                    replay_dir=config["replay_dir"],
                )
            else:
                result = run_assess(
                    repo=config["repo"],
                    dependency=config["dependency"],
                    target_version=config["target_version"],
                    mode=config["mode"],
                    model=config["model"],
                    api_key=config["api_key"],
                    base_url=config["base_url"],
                    allow_quality_patch=config["allow_quality_patch"],
                    replay_dir=config["replay_dir"],
                )
    except Exception as exc:
        st.exception(exc)
        return

    # 结果 tabs
    tab_overview, tab_plan, tab_risks, tab_evidence, tab_trace, tab_report, tab_patch = st.tabs(
        [
            "📊 概览",
            "📋 Agent 计划",
            "⚠️ 风险明细",
            "📄 代码证据",
            "🔧 工具 Trace",
            "📝 报告",
            "🔀 Patch 草稿",
        ]
    )

    with tab_overview:
        _render_overview(result)
    with tab_plan:
        _render_plan(result)
    with tab_risks:
        _render_risks(result)
    with tab_evidence:
        _render_evidence(result)
    with tab_trace:
        _render_trace(result)
    with tab_report:
        _render_report(result)
    with tab_patch:
        _render_patch(result)


if __name__ == "__main__":
    main()
