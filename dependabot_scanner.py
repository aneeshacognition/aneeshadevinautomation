#!/usr/bin/env python3
"""Dependabot backlog scanner that files Devin remediation work.

This script reads the *real upstream* Dependabot backlog and the Dependabot
ignore-list, then files de-duplicated GitHub issues on a fork so Devin can
pick the work up. Two kinds of issues are produced:

* ``dependabot-remediation`` - one per upstream Dependabot PR whose CI is stuck
  (failing checks). Devin should re-apply the version bump on the fork and fix
  whatever broke.
* ``dependabot-unblock`` - one per dependency that upstream pins in the
  Dependabot ``ignore`` list. The maintainer's own comment is quoted verbatim
  as the rationale. Devin should do the migration and remove the ignore entry.

When ``DEVIN_API_KEY`` / ``DEVIN_ORG_ID`` are set, each newly filed issue also
nudges the Devin orchestrator through the v3 API so a session starts
immediately instead of waiting for a human to triage the issue.

The script depends only on the Python standard library plus PyYAML. It talks
to GitHub through the REST API using ``GITHUB_TOKEN``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

import yaml

GITHUB_API = "https://api.github.com"
DEVIN_API = "https://api.devin.ai"

# Issue label and body-marker conventions. The markers are what make the
# scanner idempotent: an issue is only filed when no existing issue already
# carries the matching marker.
REMEDIATION_LABEL = "dependabot-remediation"
UNBLOCK_LABEL = "dependabot-unblock"
DEVIN_LABEL = "devin"

REMEDIATION_MARKER = "Upstream-PR"  # body line: "Upstream-PR: #41420"
UNBLOCK_MARKER = "Ignore-Dep"  # body line: "Ignore-Dep: react-icons"

FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required"}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    upstream_repo: str
    fork_repo: str
    github_token: str
    devin_api_key: str | None
    devin_org_id: str | None
    stuck_min_failures: int
    max_issues_per_run: int
    devin_max_acu: int | None
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Config":
        fork = os.environ.get("FORK_REPO") or os.environ.get("GITHUB_REPOSITORY")
        if not fork:
            sys.exit("FORK_REPO or GITHUB_REPOSITORY must be set")
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            sys.exit("GITHUB_TOKEN must be set")
        return cls(
            upstream_repo=os.environ.get("UPSTREAM_REPO", "apache/superset"),
            fork_repo=fork,
            github_token=token,
            devin_api_key=os.environ.get("DEVIN_API_KEY") or None,
            devin_org_id=os.environ.get("DEVIN_ORG_ID") or None,
            stuck_min_failures=int(os.environ.get("STUCK_MIN_FAILURES", "1")),
            max_issues_per_run=int(os.environ.get("MAX_ISSUES_PER_RUN", "10")),
            devin_max_acu=(
                int(os.environ["DEVIN_MAX_ACU"])
                if os.environ.get("DEVIN_MAX_ACU")
                else None
            ),
            dry_run=os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"},
        )

    @property
    def devin_enabled(self) -> bool:
        return bool(self.devin_api_key and self.devin_org_id)


# --------------------------------------------------------------------------- #
# Tiny HTTP helpers (stdlib only)
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
        with urllib.request.urlopen(req) as resp:
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


class GitHub:
    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dependabot-scanner",
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

    def post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        status, body, _ = _request(
            "POST", f"{GITHUB_API}{path}", self._headers, payload
        )
        return status, body


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().lstrip("<").rstrip(">")
        if 'rel="next"' in segments[1]:
            return url
    return None


# --------------------------------------------------------------------------- #
# Upstream backlog scanning
# --------------------------------------------------------------------------- #
@dataclass
class StuckPR:
    number: int
    title: str
    branch: str
    html_url: str
    failed_checks: list[str]
    created_at: str


def _is_dependabot(pr: dict[str, Any]) -> bool:
    user = (pr.get("user") or {}).get("login", "")
    return user in {"dependabot[bot]", "dependabot-preview[bot]"}


def find_stuck_prs(gh: GitHub, cfg: Config) -> list[StuckPR]:
    stuck: list[StuckPR] = []
    pulls = gh.paginate(
        f"/repos/{cfg.upstream_repo}/pulls?state=open&per_page=100&sort=created"
    )
    for pr in pulls:
        if not _is_dependabot(pr):
            continue
        sha = pr["head"]["sha"]
        runs = gh.get(
            f"/repos/{cfg.upstream_repo}/commits/{sha}/check-runs?per_page=100"
        )
        failed = sorted(
            {
                run["name"]
                for run in runs.get("check_runs", [])
                if (run.get("conclusion") or "").lower() in FAILED_CONCLUSIONS
            }
        )
        if len(failed) >= cfg.stuck_min_failures:
            stuck.append(
                StuckPR(
                    number=pr["number"],
                    title=pr["title"],
                    branch=pr["head"]["ref"],
                    html_url=pr["html_url"],
                    failed_checks=failed,
                    created_at=pr["created_at"],
                )
            )
    return stuck


# --------------------------------------------------------------------------- #
# Dependabot ignore-list parsing (comment-aware)
# --------------------------------------------------------------------------- #
@dataclass
class IgnoredDep:
    name: str
    rationales: list[str] = field(default_factory=list)
    directories: set[str] = field(default_factory=set)
    update_types: set[str] = field(default_factory=set)

    def add_rationale(self, text: str) -> None:
        text = text.strip()
        if text and text not in self.rationales:
            self.rationales.append(text)


_DEP_RE = re.compile(r'-\s*dependency-name:\s*["\']?([^"\'\s]+)["\']?')


def _rationales_by_dep(raw_yaml: str) -> dict[str, list[str]]:
    """Map each ``dependency-name`` to the maintainer comment that precedes it.

    PyYAML drops comments, so the rationale is recovered from the raw text by
    tracking the block of ``#`` comment lines immediately preceding each
    ``dependency-name`` entry. Consecutive entries that share a single comment
    block (a common grouping) inherit that block; a blank line resets the
    inheritance so an unrelated later entry is not misattributed.
    """
    rationales: dict[str, list[str]] = {}
    comment_buffer: list[str] = []
    last_rationale: list[str] = []

    for line in raw_yaml.splitlines():
        stripped = line.strip()
        if not stripped:
            comment_buffer = []
            last_rationale = []
            continue
        if stripped.startswith("#"):
            comment_buffer.append(stripped.lstrip("#").strip())
            continue
        dep_match = _DEP_RE.search(line)
        if dep_match:
            rationale = comment_buffer if comment_buffer else last_rationale
            last_rationale = rationale
            comment_buffer = []
            bucket = rationales.setdefault(dep_match.group(1), [])
            for text in rationale:
                if text and text not in bucket:
                    bucket.append(text)
        else:
            # Any other non-comment line breaks comment adjacency, but an
            # indented continuation of the current entry (e.g. update-types)
            # must not wipe the shared rationale for grouped siblings.
            comment_buffer = []

    return rationales


def _block_directories(block: dict[str, Any]) -> set[str]:
    dirs = set()
    if isinstance(block.get("directory"), str):
        dirs.add(block["directory"])
    for directory in block.get("directories") or []:
        if isinstance(directory, str):
            dirs.add(directory)
    return dirs or {"/"}


def parse_ignore_list(raw_yaml: str) -> dict[str, IgnoredDep]:
    """Parse the Dependabot ignore-list across every update block.

    Directories and ignored update-types come from the structured YAML (robust
    to key ordering), while the human rationale is recovered from the raw
    comments via :func:`_rationales_by_dep`.
    """
    parsed = yaml.safe_load(raw_yaml) or {}
    rationales = _rationales_by_dep(raw_yaml)
    deps: dict[str, IgnoredDep] = {}

    for block in parsed.get("updates") or []:
        directories = _block_directories(block)
        for entry in block.get("ignore") or []:
            name = entry.get("dependency-name")
            if not name:
                continue
            dep = deps.setdefault(name, IgnoredDep(name=name))
            dep.directories.update(directories)
            for update_type in entry.get("update-types") or []:
                dep.update_types.add(update_type)
            for text in rationales.get(name, []):
                dep.add_rationale(text)

    return deps


# --------------------------------------------------------------------------- #
# Issue de-duplication + creation
# --------------------------------------------------------------------------- #
def existing_markers(gh: GitHub, cfg: Config, label: str, marker: str) -> set[str]:
    seen: set[str] = set()
    pattern = re.compile(rf"^{re.escape(marker)}:\s*(.+?)\s*$", re.MULTILINE)
    issues = gh.paginate(
        f"/repos/{cfg.fork_repo}/issues?state=all&labels={label}&per_page=100"
    )
    for issue in issues:
        if "pull_request" in issue:
            continue
        match = pattern.search(issue.get("body") or "")
        if match:
            seen.add(match.group(1).lstrip("#").strip())
    return seen


def ensure_labels(gh: GitHub, cfg: Config) -> None:
    wanted = {
        REMEDIATION_LABEL: (
            "d93f0b",
            "Dependabot bump that Devin should re-apply and fix",
        ),
        UNBLOCK_LABEL: (
            "0e8a16",
            "Pinned Dependabot ignore entry for Devin to migrate",
        ),
        DEVIN_LABEL: ("5319e7", "Work routed to the Devin orchestrator"),
    }
    for name, (color, description) in wanted.items():
        status, _ = gh.post(
            f"/repos/{cfg.fork_repo}/labels",
            {"name": name, "color": color, "description": description},
        )
        if status not in (201, 422):  # 422 == already exists
            print(f"WARN: could not ensure label {name} (status {status})")


def create_issue(
    gh: GitHub, cfg: Config, title: str, body: str, labels: list[str]
) -> str | None:
    if cfg.dry_run:
        print(f"[dry-run] would create issue: {title}")
        return None
    status, body_resp = gh.post(
        f"/repos/{cfg.fork_repo}/issues",
        {"title": title, "body": body, "labels": labels},
    )
    if status != 201:
        print(f"ERROR creating issue '{title}': {status}: {body_resp}")
        disabled = status == 410 or (
            isinstance(body_resp, dict)
            and "issues has been disabled" in str(body_resp.get("message", "")).lower()
        )
        if disabled:
            print(
                f"::warning::Issues are disabled on {cfg.fork_repo}; cannot file "
                "tracking issues. Enable them under Settings -> General -> "
                "Features -> Issues, then re-run the scanner."
            )
        return None
    url = body_resp["html_url"]
    print(f"created issue: {url}")
    return url


# --------------------------------------------------------------------------- #
# Devin orchestrator nudge
# --------------------------------------------------------------------------- #
def trigger_devin(cfg: Config, prompt: str, title: str, tags: list[str]) -> None:
    if not cfg.devin_enabled:
        print("Devin credentials not set; skipping orchestrator nudge")
        return
    if cfg.dry_run:
        print(f"[dry-run] would trigger Devin session: {title}")
        return
    payload: dict[str, Any] = {
        "prompt": prompt,
        "title": title,
        "tags": tags,
        "repos": [f"https://github.com/{cfg.fork_repo}"],
    }
    if cfg.devin_max_acu:
        payload["max_acu_limit"] = cfg.devin_max_acu
    headers = {
        "Authorization": f"Bearer {cfg.devin_api_key}",
        "Content-Type": "application/json",
        "User-Agent": "dependabot-scanner",
    }
    url = f"{DEVIN_API}/v3/organizations/{cfg.devin_org_id}/sessions"
    status, resp, _ = _request("POST", url, headers, payload)
    if status >= 400:
        print(f"ERROR triggering Devin ({status}): {resp}")
    else:
        session = (resp or {}).get("session_id") or (resp or {}).get("id")
        print(f"triggered Devin session: {session}")


# --------------------------------------------------------------------------- #
# Prompt + body builders
# --------------------------------------------------------------------------- #
def remediation_body(pr: StuckPR, cfg: Config) -> str:
    checks = "\n".join(f"  - `{c}`" for c in pr.failed_checks) or "  - (none reported)"
    return f"""## Stuck Dependabot bump

Upstream PR [{cfg.upstream_repo}#{pr.number}]({pr.html_url}) is failing CI and
is not making progress.

- **Upstream PR:** {pr.html_url}
- **Branch:** `{pr.branch}`
- **Bump:** {pr.title}
- **Failing checks:**
{checks}

### What Devin should do
1. Re-create the branch `{pr.branch}` on this fork (`{cfg.fork_repo}`) and apply
   the same version bump Dependabot proposed in the upstream PR.
2. Run the build/tests, reproduce the failing checks above, and fix whatever the
   bump broke (code, types, lockfile, snapshots).
3. Push to a `{pr.branch}` branch on the fork and open a PR so CI runs. If CI
   still fails, the `dependabot-ci-failed` workflow will re-nudge automatically.
4. If the bump cannot be completed, comment here with the blocker.

<!-- machine-readable de-dup marker; do not edit -->
{REMEDIATION_MARKER}: #{pr.number}
"""


def remediation_prompt(pr: StuckPR, cfg: Config, issue_url: str | None) -> str:
    ref = issue_url or f"upstream PR {pr.html_url}"
    return (
        f"Dependabot bump is stuck on {cfg.upstream_repo} PR #{pr.number} "
        f"({pr.title}). Tracking issue: {ref}. "
        f"On the fork {cfg.fork_repo}, re-apply the bump on branch `{pr.branch}`, "
        f"reproduce the failing checks ({', '.join(pr.failed_checks) or 'CI'}), "
        f"fix what the upgrade broke (code, types, lockfile, snapshots), and open "
        f"a PR so CI runs. Follow AGENTS.md and run pre-commit before pushing. "
        f"If you cannot finish, comment on the tracking issue with the blocker."
    )


def unblock_body(dep: IgnoredDep, cfg: Config) -> str:
    if dep.rationales:
        quoted = "\n".join(f"> {line}" for line in dep.rationales)
    else:
        quoted = "> _No maintainer rationale was recorded in dependabot.yml._"
    dirs = "\n".join(f"  - `{d}`" for d in sorted(dep.directories))
    update_types = (
        f"\n- **Ignored update types:** {', '.join(sorted(dep.update_types))}"
        if dep.update_types
        else ""
    )
    return f"""## Unblock pinned dependency: `{dep.name}`

`{dep.name}` is pinned in the upstream Dependabot `ignore` list, so Dependabot
never proposes an upgrade. The maintainer's recorded rationale:

{quoted}

- **Ignored in directories:**
{dirs}{update_types}

### What Devin should do
1. Investigate whether the rationale above still holds.
2. Perform the migration needed to make the upgrade safe (e.g. the dependency or
   peer-dependency bumps the comment calls out).
3. Remove the `{dep.name}` entry from the `ignore` list in
   `.github/dependabot.yml` on the fork (`{cfg.fork_repo}`).
4. Open a PR with the migration + ignore-list removal so CI validates it.
5. If the constraint genuinely still applies, comment here explaining why and
   leave the ignore entry in place.

<!-- machine-readable de-dup marker; do not edit -->
{UNBLOCK_MARKER}: {dep.name}
"""


def unblock_prompt(dep: IgnoredDep, cfg: Config, issue_url: str | None) -> str:
    rationale = " ".join(dep.rationales) or "no recorded rationale"
    ref = issue_url or "the tracking issue"
    return (
        f"`{dep.name}` is pinned in the Dependabot ignore-list on {cfg.fork_repo}. "
        f'Maintainer rationale: "{rationale}". Tracking issue: {ref}. '
        f"Verify whether the constraint still holds, do the migration needed to "
        f"upgrade it, and remove the `{dep.name}` entry from "
        f".github/dependabot.yml. Open a PR so CI validates the change. Follow "
        f"AGENTS.md and run pre-commit before pushing. If the constraint still "
        f"applies, comment on the tracking issue explaining why instead."
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run() -> None:
    cfg = Config.from_env()
    gh = GitHub(cfg.github_token)
    print(
        f"Scanning upstream={cfg.upstream_repo} -> fork={cfg.fork_repo} "
        f"(devin={'on' if cfg.devin_enabled else 'off'}, dry_run={cfg.dry_run})"
    )
    if not cfg.dry_run:
        ensure_labels(gh, cfg)

    budget = cfg.max_issues_per_run
    filed = 0

    # 1. Stuck upstream Dependabot PRs -> remediation issues.
    stuck = find_stuck_prs(gh, cfg)
    print(f"found {len(stuck)} stuck Dependabot PR(s)")
    done_prs = existing_markers(gh, cfg, REMEDIATION_LABEL, REMEDIATION_MARKER)
    for pr in stuck:
        if budget <= 0:
            print("issue budget exhausted; stopping remediation pass")
            break
        if str(pr.number) in done_prs:
            print(f"remediation issue already exists for PR #{pr.number}; skipping")
            continue
        title = f"[dependabot-remediation] {pr.title} (upstream #{pr.number})"
        url = create_issue(
            gh,
            cfg,
            title,
            remediation_body(pr, cfg),
            [REMEDIATION_LABEL, DEVIN_LABEL],
        )
        trigger_devin(
            cfg,
            remediation_prompt(pr, cfg, url),
            title,
            ["dependabot", "dependabot-remediation", f"upstream-pr-{pr.number}"],
        )
        budget -= 1
        filed += 1

    # 2. Pinned ignore-list dependencies -> unblock issues.
    raw = gh.get(f"/repos/{cfg.upstream_repo}/contents/.github/dependabot.yml")
    raw_yaml = base64.b64decode(raw["content"]).decode()
    yaml.safe_load(raw_yaml)  # validate it parses
    deps = parse_ignore_list(raw_yaml)
    print(f"found {len(deps)} pinned dependency(ies) in the ignore-list")
    done_deps = existing_markers(gh, cfg, UNBLOCK_LABEL, UNBLOCK_MARKER)
    for name in sorted(deps):
        dep = deps[name]
        if budget <= 0:
            print("issue budget exhausted; stopping unblock pass")
            break
        if name in done_deps:
            print(f"unblock issue already exists for {name}; skipping")
            continue
        title = f"[dependabot-unblock] {name}"
        url = create_issue(
            gh,
            cfg,
            title,
            unblock_body(dep, cfg),
            [UNBLOCK_LABEL, DEVIN_LABEL],
        )
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower())
        trigger_devin(
            cfg,
            unblock_prompt(dep, cfg, url),
            title,
            ["dependabot", "dependabot-unblock", f"dep-{slug}"],
        )
        budget -= 1
        filed += 1

    if cfg.dry_run:
        print(f"done (dry-run). would file {filed} issue(s) this run.")
    else:
        print(f"done. filed {filed} issue(s) this run.")


if __name__ == "__main__":
    run()
