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
done (dry-run). would file 0 issue(s) this run.
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

> **Enable Issues on the fork.** GitHub disables the Issues tab on forks by
> default. The scanner files its tracking issues there, so if Issues are off
> it logs a `::warning::` and GitHub returns `410 Issues has been disabled`.
> Turn it on under the fork's **Settings → General → Features → Issues**, then
> re-run the scanner.

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

## Metrics & dashboard — "how do I know this is working?"

[`metrics.py`](metrics.py) (stdlib only) folds the live system state into one
snapshot and answers that question for an engineering leader. It reads three
signals:

| Signal | Source | Answers |
| --- | --- | --- |
| **Pipeline health** | recent `scan.yml` Actions runs | Is the scanner even running? Success rate, last run, last conclusion. |
| **Task status & throughput** | remediation/unblock issues on the fork | How many tasks created, open vs. closed, opened/closed per day, **median time-to-resolution (MTTR)**. |
| **Devin sessions** | Devin API (`/v1/sessions?tags=dependabot`) | Active / blocked / finished / failed sessions and **success rate** (when `DEVIN_API`/`DEVIN_ORG_ID` are set). |

It emits two things:

1. **A Markdown report in every Actions run** — written to the run's job
   summary, so a table dashboard renders right in the Actions tab. Zero infra.
2. **`metrics.json`** — which drives a **GitHub Pages dashboard**
   ([`dashboard/index.html`](dashboard/index.html), Chart.js): KPI cards, an
   open-vs-closed task chart, a Devin-session donut, and a 30-day throughput
   trend.

### Deploy the dashboard (step by step)

[`.github/workflows/metrics.yml`](.github/workflows/metrics.yml) runs hourly (+
manual dispatch), writes the job-summary report, and publishes the Pages
dashboard. One-time setup on this repo:

1. **Enable Pages.** Repo **Settings → Pages → Build and deployment → Source =
   *GitHub Actions***. (No branch to pick — the workflow uploads the artifact.)
2. **Confirm secrets exist** (Settings → Secrets and variables → Actions). The
   workflow reuses the scanner's secrets — **no new secrets needed**:
   - `FORK_TOKEN` — required, to read issues on the fork.
   - `DEVIN_API` + `DEVIN_ORG_ID` — optional; include them to show Devin session
     metrics. Without them the dashboard still renders the GitHub-only signals.
   - Optional variable `FORK_REPO` (defaults to `aneeshacognition/superset`).
3. **Run it once.** **Actions → "Orchestrator metrics dashboard" → Run
   workflow** (the workflow also fires automatically every hour).
4. **Read the two surfaces:**
   - **In-Actions report (A):** open that run → the Markdown KPI table renders in
     the run's **Summary**. Nothing to host.
   - **Pages dashboard (B):** the live URL is printed in the run's **`deploy`**
     step and also under **Settings → Pages** — typically
     `https://<owner>.github.io/<repo>/` (here
     `https://aneeshacognition.github.io/aneeshadevinautomation/`).

> **The dashboard is empty until the scanner files real issues.** Your dry-runs
> file nothing, so do one **live** scan first: set the repo variable
> `MAX_ISSUES_PER_RUN=1`, then **Actions → "Dependabot backlog scanner
> (Devin)" → Run workflow** with **`dry_run = false`**. That files one issue
> (and starts one Devin session), which then shows up on the next metrics run.
> Scale `MAX_ISSUES_PER_RUN` back up once you're happy.

### Run the metrics locally

```bash
GITHUB_TOKEN=ghp_xxx FORK_REPO=aneeshacognition/superset \
  SCANNER_REPO=aneeshacognition/aneeshadevinautomation \
  METRICS_JSON_PATH=dashboard/metrics.json python3 metrics.py
# then open dashboard/index.html (serve the folder so fetch() works):
python3 -m http.server -d dashboard 8000   # http://localhost:8000
```

> The dashboard is empty until the scanner has filed real issues. For a first
> populated view, do one live run with `MAX_ISSUES_PER_RUN=1` (a repo variable)
> to watch a single issue → Devin session → fix PR cycle before scaling up.

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
├── metrics.py                     # effectiveness metrics -> metrics.json + summary
├── dashboard/
│   └── index.html                 # Chart.js dashboard (published to Pages)
├── Dockerfile                     # python:3.11-slim, non-root, entrypoint=scanner
├── docker-compose.yml             # `docker compose run --rm scanner`
├── requirements.txt               # PyYAML
├── .env.example                   # copy to .env for local runs
├── .github/workflows/
│   ├── scan.yml                   # scheduled Dockerized scan (runs here)
│   └── metrics.yml                # hourly metrics + Pages dashboard
└── examples/
    └── dependabot-ci-failed.yml   # copy this onto the fork's master
```

## Local development

```bash
pip install -r requirements.txt
python -c "import dependabot_scanner"          # import smoke test
GITHUB_TOKEN=ghp_xxx FORK_REPO=owner/repo DRY_RUN=1 python dependabot_scanner.py
```
