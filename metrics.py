#!/usr/bin/env python3
"""Collect effectiveness metrics for the Dependabot -> Devin orchestrator.

This reads the live state of the system and answers the question an engineering
leader actually asks: *"How do I know this is working?"* It pulls three signals
and folds them into a single snapshot:

1. **Scanner pipeline health** - recent runs of the scanner workflow (success
   rate, last run, last conclusion). Is the job even running?
2. **Task status / throughput** - the remediation/unblock issues the scanner
   files on the fork: how many are open vs. closed, opened/closed per day, and
   the median time-to-resolution (MTTR).
3. **Devin sessions** - the orchestrator sessions started per task (running,
   blocked, finished, failed) when Devin credentials are available.

It emits two artifacts:

* ``metrics.json`` - machine-readable snapshot (also drives the Pages dashboard).
* a GitHub-flavoured Markdown report written to ``$GITHUB_STEP_SUMMARY`` (so a
  dashboard renders right in the Actions run) and to ``METRICS.md`` if asked.

The script depends only on the Python standard library so it can run on a bare
GitHub Actions runner without ``pip install``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

GITHUB_API = "https://api.github.com"
DEVIN_API = "https://api.devin.ai"

REMEDIATION_LABEL = "dependabot-remediation"
UNBLOCK_LABEL = "dependabot-unblock"

# Devin session status_enum values that count as terminal success / failure.
# The Devin API uses status_enum like: "RUNNING", "blocked", "finished",
# "expired", "stopped". We normalise case and bucket them.
SUCCESS_STATES = {"finished", "completed", "succeeded"}
FAILURE_STATES = {"expired", "failed", "cancelled", "error"}
ACTIVE_STATES = {"running", "working", "resuming", "starting"}
BLOCKED_STATES = {"blocked", "suspend_requested", "suspended"}

SCAN_WORKFLOW_FILE = "scan.yml"
TREND_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _day(value: str | None) -> str | None:
    ts = _parse_ts(value)
    return ts.date().isoformat() if ts else None


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    fork_repo: str
    github_token: str
    metrics_repo: str | None
    devin_api_key: str | None
    devin_org_id: str | None
    write_markdown_file: bool

    @classmethod
    def from_env(cls) -> "Config":
        fork = os.environ.get("FORK_REPO") or os.environ.get("GITHUB_REPOSITORY")
        if not fork:
            sys.exit("FORK_REPO or GITHUB_REPOSITORY must be set")
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            sys.exit("GITHUB_TOKEN must be set")
        return cls(
            fork_repo=fork,
            github_token=token,
            # Repo whose Actions runs represent the scanner pipeline. Defaults to
            # the repo this workflow runs in (the automation repo).
            metrics_repo=os.environ.get("SCANNER_REPO")
            or os.environ.get("GITHUB_REPOSITORY"),
            devin_api_key=os.environ.get("DEVIN_API_KEY") or None,
            devin_org_id=os.environ.get("DEVIN_ORG_ID") or None,
            write_markdown_file=os.environ.get("WRITE_METRICS_MD", "").lower()
            in {"1", "true", "yes"},
        )

    @property
    def devin_enabled(self) -> bool:
        return bool(self.devin_api_key and self.devin_org_id)


# --------------------------------------------------------------------------- #
# Tiny HTTP helper (stdlib only)
# --------------------------------------------------------------------------- #
def _request(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any, dict[str, str]]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            body = json.loads(raw) if raw else None
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = raw
        return exc.code, body, dict(exc.headers or {})
    except urllib.error.URLError as exc:
        return 0, str(exc), {}


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().lstrip("<").rstrip(">")
        if 'rel="next"' in segments[1]:
            return url
    return None


class GitHub:
    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dependabot-metrics",
        }

    def get(self, path: str) -> Any:
        status, body, _ = _request("GET", f"{GITHUB_API}{path}", self._headers)
        if status >= 400:
            raise RuntimeError(f"GET {path} -> {status}: {body}")
        return body

    def paginate(self, path: str) -> Iterable[Any]:
        url: str | None = f"{GITHUB_API}{path}"
        while url:
            status, body, headers = _request("GET", url, self._headers)
            if status >= 400:
                raise RuntimeError(f"GET {url} -> {status}: {body}")
            yield from body
            url = _next_link(headers.get("Link", ""))


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #
@dataclass
class IssueStat:
    total: int = 0
    open: int = 0
    closed: int = 0
    resolution_hours: list[float] = field(default_factory=list)

    def add(self, issue: dict[str, Any]) -> None:
        self.total += 1
        if issue.get("state") == "closed":
            self.closed += 1
            created = _parse_ts(issue.get("created_at"))
            closed = _parse_ts(issue.get("closed_at"))
            if created and closed:
                self.resolution_hours.append(
                    (closed - created).total_seconds() / 3600.0
                )
        else:
            self.open += 1


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 1)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 1)


def collect_issue_metrics(gh: GitHub, cfg: Config) -> dict[str, Any]:
    by_type: dict[str, IssueStat] = {
        REMEDIATION_LABEL: IssueStat(),
        UNBLOCK_LABEL: IssueStat(),
    }
    opened_by_day: Counter[str] = Counter()
    closed_by_day: Counter[str] = Counter()
    cutoff = (_now() - timedelta(days=TREND_DAYS)).date().isoformat()

    for label, stat in by_type.items():
        issues = gh.paginate(
            f"/repos/{cfg.fork_repo}/issues"
            f"?state=all&labels={urllib.parse.quote(label)}&per_page=100"
        )
        for issue in issues:
            if "pull_request" in issue:  # issues API also returns PRs
                continue
            stat.add(issue)
            opened = _day(issue.get("created_at"))
            if opened and opened >= cutoff:
                opened_by_day[opened] += 1
            if issue.get("state") == "closed":
                closed = _day(issue.get("closed_at"))
                if closed and closed >= cutoff:
                    closed_by_day[closed] += 1

    all_resolution = [h for stat in by_type.values() for h in stat.resolution_hours]
    totals = {
        "total": sum(s.total for s in by_type.values()),
        "open": sum(s.open for s in by_type.values()),
        "closed": sum(s.closed for s in by_type.values()),
        "mttr_hours": _median(all_resolution),
    }
    return {
        "totals": totals,
        "by_type": {
            "remediation": _stat_dict(by_type[REMEDIATION_LABEL]),
            "unblock": _stat_dict(by_type[UNBLOCK_LABEL]),
        },
        "trend": _build_trend(opened_by_day, closed_by_day),
    }


def _stat_dict(stat: IssueStat) -> dict[str, Any]:
    return {
        "total": stat.total,
        "open": stat.open,
        "closed": stat.closed,
        "mttr_hours": _median(stat.resolution_hours),
    }


def _build_trend(opened: Counter[str], closed: Counter[str]) -> list[dict[str, Any]]:
    today = _now().date()
    days = [
        (today - timedelta(days=i)).isoformat() for i in range(TREND_DAYS - 1, -1, -1)
    ]
    return [
        {"date": d, "opened": opened.get(d, 0), "closed": closed.get(d, 0)}
        for d in days
    ]


def collect_pipeline_health(gh: GitHub, cfg: Config) -> dict[str, Any]:
    if not cfg.metrics_repo:
        return {"available": False}
    try:
        data = gh.get(
            f"/repos/{cfg.metrics_repo}/actions/workflows/"
            f"{SCAN_WORKFLOW_FILE}/runs?per_page=50"
        )
    except RuntimeError:
        return {"available": False}
    runs = data.get("workflow_runs", [])
    completed = [r for r in runs if r.get("status") == "completed"]
    successes = [r for r in completed if r.get("conclusion") == "success"]
    last = runs[0] if runs else None
    success_rate = round(100 * len(successes) / len(completed)) if completed else None
    return {
        "available": True,
        "runs_considered": len(runs),
        "completed": len(completed),
        "successes": len(successes),
        "success_rate_pct": success_rate,
        "last_run_at": last.get("run_started_at") if last else None,
        "last_conclusion": last.get("conclusion") if last else None,
        "last_run_url": last.get("html_url") if last else None,
    }


def _bucket_session(status: str) -> str:
    status = (status or "").lower()
    if status in SUCCESS_STATES:
        return "finished"
    if status in FAILURE_STATES:
        return "failed"
    if status in BLOCKED_STATES:
        return "blocked"
    if status in ACTIVE_STATES:
        return "active"
    return "other"


def collect_devin_metrics(cfg: Config) -> dict[str, Any]:
    if not cfg.devin_enabled:
        return {"available": False, "reason": "Devin credentials not set"}
    headers = {
        "Authorization": f"Bearer {cfg.devin_api_key}",
        "Content-Type": "application/json",
        "User-Agent": "dependabot-metrics",
    }
    sessions: list[dict[str, Any]] = []
    # The list endpoint is paginated by limit/offset; tagged sessions only.
    offset = 0
    for _ in range(20):  # hard cap: 20 pages
        url = f"{DEVIN_API}/v1/sessions?limit=100&offset={offset}&tags=dependabot"
        status, body, _h = _request("GET", url, headers)
        if status >= 400:
            return {
                "available": False,
                "reason": f"Devin API returned {status}",
            }
        page = (body or {}).get("sessions") or (body or {}).get("data") or []
        if not page:
            break
        sessions.extend(page)
        if len(page) < 100:
            break
        offset += 100

    buckets: Counter[str] = Counter()
    for session in sessions:
        state = (
            session.get("status_enum")
            or session.get("status")
            or session.get("state")
            or ""
        )
        buckets[_bucket_session(state)] += 1

    terminal = buckets["finished"] + buckets["failed"]
    success_rate = round(100 * buckets["finished"] / terminal) if terminal else None
    return {
        "available": True,
        "total": len(sessions),
        "active": buckets["active"],
        "blocked": buckets["blocked"],
        "finished": buckets["finished"],
        "failed": buckets["failed"],
        "other": buckets["other"],
        "success_rate_pct": success_rate,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _fmt_hours(hours: float | None) -> str:
    if hours is None:
        return "n/a"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _fmt_pct(pct: int | None) -> str:
    return "n/a" if pct is None else f"{pct}%"


def _fmt_ts(value: str | None) -> str:
    ts = _parse_ts(value)
    return ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "never"


def render_markdown(snapshot: dict[str, Any]) -> str:
    pipe = snapshot["pipeline"]
    issues = snapshot["issues"]
    devin = snapshot["devin"]
    t = issues["totals"]

    lines: list[str] = []
    lines.append("# Dependabot -> Devin orchestrator — effectiveness")
    lines.append("")
    lines.append(f"_Snapshot: {_fmt_ts(snapshot['generated_at'])}_")
    lines.append("")

    # KPI table
    lines.append("## At a glance")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Tasks created (total) | **{t['total']}** |")
    lines.append(f"| Open / Closed | {t['open']} / {t['closed']} |")
    lines.append(f"| Median time-to-resolution | {_fmt_hours(t['mttr_hours'])} |")
    if devin.get("available"):
        lines.append(
            f"| Devin sessions (active/blocked/done/failed) | "
            f"{devin['active']} / {devin['blocked']} / "
            f"{devin['finished']} / {devin['failed']} |"
        )
        lines.append(f"| Devin success rate | {_fmt_pct(devin['success_rate_pct'])} |")
    if pipe.get("available"):
        lines.append(
            f"| Scanner success rate (last {pipe['completed']} runs) | "
            f"{_fmt_pct(pipe['success_rate_pct'])} |"
        )
        lines.append(
            f"| Last scan | {_fmt_ts(pipe['last_run_at'])} "
            f"({pipe['last_conclusion'] or 'n/a'}) |"
        )
    lines.append("")

    # Task breakdown
    rem = issues["by_type"]["remediation"]
    unb = issues["by_type"]["unblock"]
    lines.append("## Tasks by type")
    lines.append("")
    lines.append("| Type | Total | Open | Closed | MTTR |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.append(
        f"| Remediation (stuck PRs) | {rem['total']} | {rem['open']} | "
        f"{rem['closed']} | {_fmt_hours(rem['mttr_hours'])} |"
    )
    lines.append(
        f"| Unblock (pinned deps) | {unb['total']} | {unb['open']} | "
        f"{unb['closed']} | {_fmt_hours(unb['mttr_hours'])} |"
    )
    lines.append("")

    if not devin.get("available"):
        lines.append(
            f"> Devin session metrics unavailable: {devin.get('reason', 'unknown')}."
        )
        lines.append("")

    # Recent throughput (last 7 days from the trend)
    recent = snapshot["issues"]["trend"][-7:]
    opened = sum(d["opened"] for d in recent)
    closed = sum(d["closed"] for d in recent)
    lines.append(
        f"## Throughput (last 7 days): **{opened} opened**, **{closed} closed**"
    )
    lines.append("")
    if pipe.get("available") and pipe.get("last_run_url"):
        lines.append(f"[Latest scanner run]({pipe['last_run_url']})")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_snapshot(gh: GitHub, cfg: Config) -> dict[str, Any]:
    return {
        "generated_at": _now().isoformat(),
        "fork_repo": cfg.fork_repo,
        "pipeline": collect_pipeline_health(gh, cfg),
        "issues": collect_issue_metrics(gh, cfg),
        "devin": collect_devin_metrics(cfg),
    }


def run() -> None:
    cfg = Config.from_env()
    gh = GitHub(cfg.github_token)
    print(
        f"Collecting metrics for fork={cfg.fork_repo} "
        f"(devin={'on' if cfg.devin_enabled else 'off'})"
    )
    snapshot = build_snapshot(gh, cfg)

    out_json = os.environ.get("METRICS_JSON_PATH", "metrics.json")
    with open(out_json, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2)
    print(f"wrote {out_json}")

    markdown = render_markdown(snapshot)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
        print("wrote job summary")

    if cfg.write_markdown_file:
        with open("METRICS.md", "w", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
        print("wrote METRICS.md")

    # Always echo to stdout so a plain run (no Actions) still shows the report.
    print("\n" + markdown)


if __name__ == "__main__":
    run()
