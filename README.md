# Dependabot → Devin orchestrator

Automatically turn a project's **real Dependabot backlog** into Devin work.

This repo holds the portable, Dockerized **scanner** plus the GitHub Actions
glue. It points at an *upstream* repository (default `apache/superset`), reads
the live Dependabot state, and files de-duplicated tracking issues on a *fork*
you control — then nudges the Devin orchestrator so a session starts on each
one immediately.

It produces two kinds of work:

| Issue type | Trigger | What Devin does |
| --- | --- | --- |
| `dependabot-remediation` | An **open upstream Dependabot PR whose CI is stuck** (failing checks). | Re-applies the same version bump on the fork, reproduces the failing checks, fixes what the bump broke (code, types, lockfile, snapshots), and opens a PR. |
| `dependabot-unblock` | A dependency **pinned in the upstream Dependabot `ignore` list**. The maintainer's own comment is quoted verbatim as the rationale. | Verifies whether the constraint still holds, performs the migration, removes the `ignore` entry from `.github/dependabot.yml`, and opens a PR. |

A third workflow (`examples/dependabot-ci-failed.yml`, deployed **on the fork**)
closes the loop: the moment CI fails on a `dependabot/*` branch, it nudges the
orchestrator to fix it.

---

## How it fits together

```
          ┌─────────────────────────┐        files issues + triggers Devin
          │  upstream (apache/...)   │                     │
          │  • open Dependabot PRs   │                     ▼
          │  • dependabot.yml ignore │        ┌──────────────────────────┐
          └────────────┬─────────────┘        │  fork (you control)      │
                       │ read (this scanner)   │  • tracking issues       │
                       ▼                        │  • Devin pushes fixes    │
        ┌────────────────────────────┐         │  • dependabot-ci-failed  │
        │  scanner (Docker / Action) │────────▶│    re-nudges on CI fail  │
        └────────────────────────────┘         └──────────────────────────┘
```

- **Idempotent.** Every issue carries a hidden marker (`Upstream-PR: #41420`
  or `Ignore-Dep: react-icons`). Before filing, the scanner reads existing
  issues with the matching label and skips anything already tracked, so re-runs
  never create duplicates.
- **Comment-aware.** PyYAML drops comments, so the maintainer rationale for each
  pinned dependency is recovered from the raw `dependabot.yml` text and quoted
  verbatim in the unblock issue.
- **Scoped.** The CI-failed nudge only fires for failures a code change can fix
  (`test`, `lint`, `mypy`, `eslint`, `prettier`, `type`, `build`, `frontend`,
  `python`, `pre-commit`) — not pure infra/Docker timeouts.

---

## Quick start (run or simulate locally)

### Option A — Docker Compose (simplest)

```bash
cp .env.example .env        # then edit .env (see variables below)
docker compose run --rm scanner
```

`.env` defaults to `DRY_RUN=1`, so this **simulates** a run: it prints every
issue it *would* file and every Devin session it *would* start, without
touching GitHub or Devin. Set `DRY_RUN=0` in `.env` for a real run.

### Option B — plain Docker

```bash
docker build -t dependabot-scanner .

# Dry run (simulate) — only needs a read token:
docker run --rm \
  -e GITHUB_TOKEN=ghp_xxx \
  -e UPSTREAM_REPO=apache/superset \
  -e FORK_REPO=aneeshacognition/superset \
  -e DRY_RUN=1 \
  dependabot-scanner

# Real run — files issues on the fork (and triggers Devin if creds are set):
docker run --rm \
  -e GITHUB_TOKEN=ghp_xxx \
  -e UPSTREAM_REPO=apache/superset \
  -e FORK_REPO=aneeshacognition/superset \
  -e DEVIN_API_KEY=$DEVIN_API \
  -e DEVIN_ORG_ID=org-8148d42f139c44e39fac4d401c198130 \
  -e DRY_RUN=0 \
  dependabot-scanner
```

### Option C — no Docker

```bash
pip install -r requirements.txt
GITHUB_TOKEN=ghp_xxx FORK_REPO=aneeshacognition/superset DRY_RUN=1 \
  python dependabot_scanner.py
```

### Example dry-run output

```
Scanning upstream=apache/superset -> fork=aneeshacognition/superset (devin=off, dry_run=True)
found 22 stuck Dependabot PR(s)
[dry-run] would create issue: [dependabot-remediation] Bump ... (upstream #41420)
...
found 9 pinned dependency(ies) in the ignore-list
[dry-run] would create issue: [dependabot-unblock] react-icons
...
done. filed 0 issue(s) this run.
```

---

## Configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | yes | – | Reads upstream backlog; files issues on `FORK_REPO`. Needs `issues:write` on the fork for a real run; read-only is fine for `DRY_RUN=1`. |
| `FORK_REPO` | yes | – | `owner/repo` where issues are filed and Devin pushes fixes. (`GITHUB_REPOSITORY` is used as a fallback.) |
| `UPSTREAM_REPO` | no | `apache/superset` | `owner/repo` whose Dependabot backlog is scanned. |
| `STUCK_MIN_FAILURES` | no | `1` | Number of failing checks before a PR counts as "stuck". |
| `MAX_ISSUES_PER_RUN` | no | `10` | Safety cap on issues filed per run (across both types). |
| `DEVIN_API_KEY` | no | – | Devin service-user token (`ManageOrgSessions`). When set with `DEVIN_ORG_ID`, each new issue starts a Devin session. |
| `DEVIN_ORG_ID` | no | – | Devin organization ID (prefix `org-`). |
| `DEVIN_MAX_ACU` | no | – | Per-session ACU cap for triggered sessions. |
| `DRY_RUN` | no | `false` | `1`/`true` lists actions without filing issues or starting sessions. |

If `DEVIN_API_KEY`/`DEVIN_ORG_ID` are absent the scanner still files/triages
issues and just skips the Devin trigger (logs "credentials not set"), so it is
safe to roll out issue-filing first and add the Devin nudge later.

---

## Deploying as GitHub Actions

Two pieces, two repos:

### 1. The scanner — runs from *this* repo

[`.github/workflows/scan.yml`](.github/workflows/scan.yml) builds the Docker
image and runs it every 6 hours (and on manual dispatch with a dry-run toggle).

Because the built-in `GITHUB_TOKEN` can only touch *this* repo, the scanner
needs a token that can file issues on the fork. Add these in this repo's
**Settings → Secrets and variables → Actions**:

- **Secrets:**
  - `FORK_TOKEN` — PAT with `issues:write` on the fork (classic PAT with `repo`
    /`public_repo`, or a fine-grained PAT scoped to the fork with
    Issues: read/write).
  - `DEVIN_API` — Devin service-user token (optional, enables the nudge).
  - `DEVIN_ORG_ID` — your Devin org ID (optional).
- **Variables (optional):** `UPSTREAM_REPO`, `FORK_REPO`, `STUCK_MIN_FAILURES`,
  `MAX_ISSUES_PER_RUN`, `DEVIN_MAX_ACU`.

> Prefer running the scanner directly from the fork instead? Drop
> `dependabot_scanner.py` into the fork and call it from a scheduled workflow
> there — then the fork's own `GITHUB_TOKEN` (with `issues: write`) is enough
> and no PAT is needed.

### 2. The CI-failed nudge — must run *on the fork*

[`examples/dependabot-ci-failed.yml`](examples/dependabot-ci-failed.yml) reacts
to `check_suite: completed` on `dependabot/*` branches. `check_suite`-triggered
workflows only arm from a repo's **default branch**, and the events fire in the
repo where Dependabot runs CI — so this file must be committed to the fork's
`master` (it cannot live here). Copy it to
`.github/workflows/dependabot-ci-failed.yml` on the fork and add the `DEVIN_API`
+ `DEVIN_ORG_ID` secrets there.

---

## Setting up the Devin secrets

1. **Create a service user** at app.devin.ai → **Settings → Service Accounts**,
   give it the `ManageOrgSessions` permission, and generate its API key. Using a
   service account (not your personal key) keeps the automation independent of
   any one person.
2. Store that key as the `DEVIN_API` secret (and `FORK_TOKEN`/`DEVIN_ORG_ID` as
   above).
3. Your Devin org ID has the form `org-...` and is shown in the Devin app
   settings.

---

## Repository layout

```
.
├── dependabot_scanner.py          # the scanner (stdlib + PyYAML)
├── Dockerfile                     # python:3.11-slim, non-root, entrypoint=scanner
├── docker-compose.yml             # `docker compose run --rm scanner`
├── requirements.txt               # PyYAML
├── .env.example                   # copy to .env for local runs
├── .github/workflows/scan.yml     # scheduled Dockerized scan (runs here)
└── examples/
    └── dependabot-ci-failed.yml   # copy this onto the fork's master
```

## Local development

```bash
pip install -r requirements.txt
python -c "import dependabot_scanner"          # import smoke test
GITHUB_TOKEN=ghp_xxx FORK_REPO=owner/repo DRY_RUN=1 python dependabot_scanner.py
```
