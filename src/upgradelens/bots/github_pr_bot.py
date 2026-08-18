"""GitHub PR bot: run PR review + security review on a PR and post the report (M4).

This is the M4 "GitHub bot" deliverable. It reuses the unified capabilities
(:func:`upgradelens.capabilities.workbench.run_capability`) so the report is the
exact same evidence the Workbench produces -- only the *delivery* differs
(posting a comment instead of rendering HTML).

Live verification (real GitHub API + a real LLM) is deferred. The bot supports a
``--dry-run`` flag that prints the report instead of posting, and the capability
runs default to the ``fake`` gateway so the whole pipeline can be exercised
offline (e.g. in CI) without any network or API key.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Any

from upgradelens.capabilities.workbench import run_capability
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind
from upgradelens.tools.fetcher import RestrictedFetcher
from upgradelens.tools.github import GitHubClient
from upgradelens.tools.trace import ToolTrace


def _run_capability(kind: TaskKind, repo_root: str, diff: str, *, mode: str) -> dict[str, Any]:
    ctx = TaskContext(repo=repo_root, unified_diff=diff)
    task = SoftwareTask(
        task_id=f"bot-{kind.value}",
        kind=kind,
        goal=f"{kind.value} (PR bot)",
        context=ctx,
    )
    result = run_capability(task, mode=mode)
    return result.model_dump(mode="json")


def build_pr_report(repo_root: str, diff: str, *, mode: str = "fake") -> str:
    """Run PR review + security review on ``diff`` and render a Markdown report.

    Offline-friendly: with ``mode="fake"`` and an empty or placeholder diff this
    runs the deterministic analyzers + a canned LLM classification, no network.
    """
    pr = _run_capability(TaskKind.PR_REVIEW, repo_root, diff, mode=mode)
    sec = _run_capability(TaskKind.SECURITY_REVIEW, repo_root, diff, mode=mode)
    return _render_report(pr, sec)


def _render_report(pr: dict[str, Any], sec: dict[str, Any]) -> str:
    lines: list[str] = ["## UpgradeLens PR 审查报告", ""]
    for title, res in (("### PR 审查", pr), ("### 安全审查", sec)):
        lines.append(title)
        lines.append("")
        lines.append(f"- 状态: {res.get('status', 'unknown')}")
        if res.get("summary"):
            lines.append(f"- 摘要: {res['summary']}")
        verification = res.get("verification") or {}
        if verification.get("passed") is not None:
            lines.append(f"- 验证闸门: {'通过' if verification['passed'] else '未通过'}")
        findings = res.get("findings") or []
        if findings:
            lines.append("")
            lines.append("发现：")
            for f in findings:
                if isinstance(f, dict):
                    sev = f.get("severity", "")
                    msg = f.get("message", f.get("title", f))
                else:
                    sev, msg = "", str(f)
                lines.append(f"- [{sev}] {msg}")
        lines.append("")
    return "\n".join(lines)


def run(
    repo_slug: str,
    pr_number: int,
    *,
    token: str | None = None,
    mode: str = "fake",
    dry_run: bool = False,
    repo_root: str | None = None,
) -> str:
    """Fetch the PR diff, run the two reviews, and post (or print) the report.

    Args:
        repo_slug: ``owner/repo`` for the PR's repository.
        pr_number: The pull request number.
        token: GitHub token (a throwaway traced fetcher is used for the read).
        mode: Capability gateway mode (fake | live | replay).
        dry_run: Print the report instead of posting a comment.
        repo_root: Local checkout of the PR head; if omitted the report still runs
            against the diff alone over a temp dir (offline-safe).
    """
    fetcher = RestrictedFetcher(trace=ToolTrace())
    diff = GitHubClient(fetcher).pr_diff(repo_slug, pr_number, token=token)

    root = repo_root
    if root is None:
        # Best-effort: a real deployment would clone the PR head here. We keep a
        # temp dir so the capability runs never fail solely due to a missing
        # checkout in offline/dry-run scenarios.
        root = tempfile.mkdtemp(prefix="upgradelens-pr-")

    report = build_pr_report(root, diff, mode=mode)

    if dry_run:
        print(report)
        return report

    GitHubClient(fetcher).comment_pr(repo_slug, pr_number, report, token=token)
    return report


def _load_dotenv(path: str = ".env") -> None:
    """Best-effort load of ``.env`` into ``os.environ`` (stdlib only).

    The bot reads ``GITHUB_TOKEN`` from the environment; this lets users drop the
    token into the existing (gitignored) ``.env`` instead of exporting it or
    passing it on the command line. Mirrors ``eval/retrieval_baseline.py``.
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="UpgradeLens GitHub PR bot")
    parser.add_argument("--repo", required=True, help="owner/repo slug")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument("--token", default=None, help="GitHub token (or GITHUB_TOKEN env)")
    parser.add_argument(
        "--mode",
        default="fake",
        choices=["fake", "live", "replay"],
        help="capability gateway mode (default: fake, offline)",
    )
    parser.add_argument("--repo-root", default=None, help="local checkout of the PR head")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the report instead of posting a comment",
    )
    args = parser.parse_args(argv)
    token = args.token or os.environ.get("GITHUB_TOKEN")
    run(
        args.repo,
        args.pr,
        token=token,
        mode=args.mode,
        dry_run=args.dry_run,
        repo_root=args.repo_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
