# Plan: Dependency Update Agent on AWS AgentCore

> Vibecoding guide — follow step by step, deploy to AWS, connect to GitHub.
> **Revision 3** — updated to match current repo structure and implementation.

---

## Architecture

```
EventBridge Scheduler (weekly cron)
        │
        ▼
Lambda trigger ── InvokeAgentRuntime ──┐
                                       ▼
                    AgentCore Runtime (ARM64 microVM, custom container)
                    ┌──────────────────────────────────────────────────────┐
                    │ Node 26 · pnpm · git · gh · Python 3.13              │
                    ├──────────────────────────────────────────────────────┤
                    │ 1. Fetch GitHub token (Secrets Manager)              │
                    │ 2. git clone (shallow)                               │
                    │ 3. Detect & match project pnpm version               │
                    │ 4. pnpm install --frozen-lockfile (fallback: unfrozen)│
                    │ 5. Snapshot packages (before) + pnpm audit           │
                    │ 6. pnpm update  →  pnpm install (reconcile lockfile) │
                    │ 7. Snapshot packages (after) + diff                  │
                    │ 8. Lint → Format → Typecheck → Test                  │
                    │        │                                             │
                    │   PASS │                          FAIL               │
                    │        ▼                            ▼                │
                    │   create PR              Strands coding agent        │
                    │  (deterministic,        shell/read/write/grep        │
                    │   rich body)            iterate ≤3 → PR or bail      │
                    └──────────────────────────────────────────────────────┘
                                       │
                                       ▼
                             Return {status, pr_url, metrics}
```

**Stack**: Python 3.13 · Strands Agents · Claude Sonnet 4 (Bedrock) · AgentCore Runtime
(container, CDK-managed) · Node 26 · pnpm · gh CLI

**Region**: `us-east-1`

**Cost model**: $0 LLM tokens on the happy path. Claude is invoked only when tests fail.

---

## Project Structure

```
dep-update-agent/
├── .nvmrc                          # 26
├── .python-version                 # 3.13
├── .gitignore
├── .env.local                      # local secrets (gitignored)
├── README.md
├── docs/
│   └── plan.md                     # this file
└── dependencyUpdateAgent/          # AgentCore project root
    ├── AGENTS.md                   # AI assistant context (CLI-generated)
    ├── README.md
    ├── agentcore/
    │   ├── agentcore.json          # project config (schema-first, CDK-managed)
    │   ├── aws-targets.json        # deployment targets
    │   ├── .env.local              # AgentCore local env (gitignored)
    │   ├── .llm-context/           # TypeScript type defs for AI assistants
    │   └── cdk/                    # CDK infra (auto-synth from agentcore.json)
    │       ├── bin/
    │       ├── lib/
    │       ├── package.json
    │       └── ...
    └── app/
        └── depUpdateAgent/
            ├── main.py             # agent entrypoint
            ├── pyproject.toml      # Python deps (pinned)
            ├── uv.lock             # lockfile
            ├── Dockerfile          # ARM64 container image
            └── ca/                 # optional corporate proxy certs
                └── .keep
```

---

## Configuration (`agentcore.json`)

The CLI generated this via `agentcore create`. Key settings:

| Field | Value | Notes |
|---|---|---|
| `name` | `dependencyUpdateAgent` | |
| `managedBy` | `CDK` | infra via `agentcore/cdk/` |
| `build` | `Container` | custom Dockerfile |
| `runtimeVersion` | `PYTHON_3_14` | AgentCore runtime marker |
| `networkMode` | `PUBLIC` | required for GitHub access |
| `idleRuntimeSessionTimeout` | 300 s | reap after run ends |
| `maxLifetime` | 3600 s | ceiling for slow repo + fix loop |

---

## Prerequisites

- [x] AWS account, credentials configured (`aws configure` or `aws sso login` — region `us-east-1`)
- [x] **Node.js 26** (pinned in `.nvmrc`)
- [x] Python 3.13+ (pinned in `.python-version`)
- [x] `uv` installed
- [ ] Docker with buildx (for ARM64 builds — native on Apple Silicon)
- [ ] `gh` CLI authenticated locally: `gh auth login`
- [ ] Claude Sonnet 4 enabled in Bedrock → Model access (`us-east-1`)
- [ ] **A target repo to run against.** Needs a `pnpm-lock.yaml` and a `test` script.

---

## Phase 0: Local Environment — DONE

- [x] Node 26 pinned (`.nvmrc`)
- [x] Python 3.13 pinned (`.python-version`)
- [x] AgentCore CLI installed (`npm install -g @aws/agentcore`)
- [x] Project scaffolded via `agentcore create` (type: Agent / Bring your own code / Container)

---

## Phase 1: The Agent Code — DONE

All files live under `dependencyUpdateAgent/app/depUpdateAgent/`.

### 1.1 `main.py`

The entrypoint implements:

**Config** (env vars):
- `MODEL_ID` — default `us.anthropic.claude-sonnet-4-6`
- `GITHUB_SECRET_ID` — default `dep-agent/github-pat`
- `TEST_TIMEOUT` — default `600` seconds

**Tools** (for the Strands fix agent):
- `shell` — run any command in the checkout
- `read_file` — read a repo-relative file (confined by `_safe_path`)
- `write_file` — overwrite a repo-relative file (confined by `_safe_path`)
- `find_files` — glob search, skipping `node_modules`
- `grep_code` — regex search across source files

**Pipeline functions**:
- `get_github_token()` — reads from Secrets Manager, supports PAT or GitHub App
- `_installation_token()` — JWT exchange for GitHub App tokens
- `clone_repo()` — shallow clone, scrubs token from `.git/config`
- `_detect_pnpm_version()` / `_ensure_pnpm_version()` — matches project's expected pnpm
- `install_deps()` — `--frozen-lockfile` with graceful fallback
- `run_audit()` / `count_vulns()` / `extract_advisories()` — audit parsing
- `snapshot_lockfile_packages()` / `diff_packages()` — before/after package diffing
- `update_packages()` — `pnpm update` + lockfile reconciliation
- `run_tests()` / `run_lint()` / `run_format()` / `run_typecheck()` — validation suite
- `existing_pr()` — idempotency check (filters by `deps/update-*` branch prefix)
- `create_pr()` — branch, commit, push, open PR with `--body-file`
- `_build_pr_body()` — rich markdown: security table, upgraded packages, validations

**Entrypoint** (`dep_update`):
1. Clone → detect pnpm → install (frozen)
2. Snapshot before → audit before
3. `pnpm update` → check for changes
4. Snapshot after → audit after → diff packages → identify fixed advisories
5. Re-install → lint → format → typecheck → test
6. If tests fail and `allow_fixes=True`: invoke Strands fix agent (up to `max_fix_attempts`)
7. Re-run lint/format/typecheck after fix
8. Idempotency check → build PR body → create PR
9. Return structured result with metrics

**Credential scrubbing**: all exception paths replace `token` with `***` in output.

### 1.2 `pyproject.toml`

```toml
[project]
name = "dep-update-agent"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "bedrock-agentcore>=1.21.0",
    "boto3>=1.43.72",
    "pyjwt[crypto]>=2.13.0",
    "requests>=2.34.2",
    "strands-agents>=1.52.0",
]
```

### 1.3 `Dockerfile` (ARM64, Node 26)

Key details of the current Dockerfile:
- Base: `python:3.13-slim-bookworm` (ARM64)
- Optional corporate proxy CA injection (`ca/` directory)
- `gh` CLI installed from GitHub release deb (pinned `GH_VERSION=2.97.0`)
- Node 26 from official binary tarball (pinned `NODE_VERSION=26.6.0`)
- pnpm via `npm install -g` (pinned `PNPM_VERSION=11.21.0`) — Node 26 no longer ships corepack
- Python deps via `uv export` → `pip install`
- `CMD ["opentelemetry-instrument", "python", "main.py"]`

### 1.4 Local smoke test

```bash
cd dependencyUpdateAgent/app/depUpdateAgent

docker build -t dep-update-agent:local .

docker run --rm -p 8080:8080 \
  -e AWS_REGION=us-east-1 \
  -e GITHUB_SECRET_ID=dep-agent/github-pat \
  -v ~/.aws:/root/.aws:ro \
  dep-update-agent:local
```

Then in another terminal:

```bash
curl -s localhost:8080/ping
curl -s -X POST localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"repo_url":"https://github.com/<OWNER>/<TARGET_REPO>.git","allow_fixes":false}' | jq
```

Start with `"allow_fixes": false` so the first run costs zero LLM tokens.

---

## Phase 2: Verify the Pipeline Before Spending on the LLM

Work through these in order:

- [ ] `/ping` returns 200 — the SDK server is up
- [ ] Token is read from Secrets Manager (create the secret first, Phase 4)
- [ ] `git clone` succeeds and the token is **not** in `.git/config`
- [ ] pnpm version detection works (check logs for `[dep-agent] project expects pnpm...`)
- [ ] `pnpm install --frozen-lockfile` succeeds (or falls back gracefully)
- [ ] `pnpm audit --json` parses
- [ ] `pnpm update` produces a lockfile diff (`has_changes` is true)
- [ ] Package diff appears in logs (`X package(s) changed`)
- [ ] Lint/format/typecheck run (or skip gracefully if no scripts)
- [ ] `pnpm test` runs
- [ ] `git commit` succeeds — proves the container git identity is configured
- [ ] PR is created with the rich body (security table, package list, validations)
- [ ] Run it **twice**: the second run must return `pr_already_open`

Only once all pass, flip `allow_fixes` to `true` and deliberately break a test.

---

## Phase 3: Deploy to AWS

### 3.1 Deploy via AgentCore CLI

Since the project is CDK-managed:

```bash
cd dependencyUpdateAgent
agentcore deploy
agentcore status
```

The CLI builds the container in CodeBuild (ARM64), pushes to a per-agent ECR repo, and
deploys via CloudFormation.

### 3.2 Manual ECR push (alternative)

If you prefer to build locally:

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
REPO=dep-update-agent

aws ecr create-repository --repository-name $REPO --region $REGION

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

docker build --platform linux/arm64 \
  -t $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest \
  dependencyUpdateAgent/app/depUpdateAgent/

docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest
```

### 3.3 Session lifetime

Already configured in `agentcore.json`:

| Setting | Value | Why |
|---|---|---|
| `idleRuntimeSessionTimeout` | 300 s | Reap the microVM after run ends |
| `maxLifetime` | 3600 s | Ceiling for slow repo + 3 fix attempts |
| `TEST_TIMEOUT` (env) | 600 s | Must be well under `maxLifetime` |

### 3.4 IAM for the runtime execution role

```
bedrock:InvokeModel                     on the Claude model AND inference profile
bedrock:InvokeModelWithResponseStream   ditto
secretsmanager:GetSecretValue           on dep-agent/*
logs:CreateLogStream, logs:PutLogEvents on the agent's log group
xray:PutTraceSegments                   if OTel is enabled
```

> **Gotcha**: `us.anthropic.claude-sonnet-4-6` is a cross-region inference profile. The
> policy needs `bedrock:InvokeModel` on both the profile ARN **and** the underlying
> foundation-model ARNs. If you get `AccessDeniedException`, this is almost always why.

### 3.5 Invoke

```bash
agentcore invoke '{"repo_url":"https://github.com/<OWNER>/<TARGET_REPO>.git","allow_fixes":false}'
```

---

## Phase 4: GitHub Connection

### 4.1 Start with a fine-grained PAT

Create at https://github.com/settings/personal-access-tokens/new
- Repository access: **only** the target repo
- Permissions: Contents **R/W**, Pull requests **R/W**, Metadata **R**
- Expiry: 90 days

```bash
aws secretsmanager create-secret \
  --name dep-agent/github-pat \
  --region us-east-1 \
  --secret-string '{"token":"github_pat_..."}'
```

### 4.2 Move to a GitHub App when you go multi-repo

1. https://github.com/settings/apps/new
2. Permissions: Contents R/W, Pull requests R/W, Metadata R
3. Install on target repos, note the **installation ID**
4. Generate a private key (`.pem`)

```bash
aws secretsmanager create-secret \
  --name dep-agent/github-app \
  --region us-east-1 \
  --secret-string "$(jq -n \
    --arg app_id "123456" \
    --arg inst_id "78901234" \
    --arg key "$(cat dep-update-agent.private-key.pem)" \
    '{app_id:$app_id, installation_id:$inst_id, private_key:$key}')"
```

Then set `GITHUB_SECRET_ID=dep-agent/github-app`. The `get_github_token` function branches
on the secret shape automatically.

### 4.3 Branch protection

The agent pushes `deps/*` branches and opens PRs but **cannot merge**. A human reviews and
merges. Keep branch protection on `main`.

---

## Phase 5: Scheduling

### 5.1 Lambda trigger

```python
# lambda_trigger.py
import json
import os
import boto3

RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
REPOS = json.loads(os.environ.get("REPOS", "[]"))


def handler(event, context):
    client = boto3.client("bedrock-agentcore")
    results = []
    for repo in REPOS:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=f"dep-update-{abs(hash(repo))}",
            payload=json.dumps({"repo_url": repo}),
        )
        results.append({"repo": repo, "status": resp.get("statusCode")})
    return results
```

> Confirm `invoke_agent_runtime` parameter names against your boto3 version — this API
> surface is young and parameter names have shifted.

Give the Lambda a 15-minute timeout, or invoke asynchronously.

### 5.2 EventBridge rule

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1

aws events put-rule \
  --name dep-update-weekly \
  --schedule-expression "cron(0 13 ? * MON *)" \
  --state ENABLED

aws events put-targets --rule dep-update-weekly \
  --targets "[{\"Id\":\"trigger\",\"Arn\":\"arn:aws:lambda:$REGION:$ACCOUNT:function:dep-agent-trigger\"}]"

aws lambda add-permission \
  --function-name dep-agent-trigger \
  --statement-id events-invoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:$REGION:$ACCOUNT:rule/dep-update-weekly
```

---

## Phase 6: Observability & Notifications

### 6.1 Enable Transaction Search

Console → CloudWatch → Application Signals → Transaction Search (once per account).

### 6.2 SNS notification

```python
sns = boto3.client("sns")
sns.publish(
    TopicArn=os.environ["NOTIFY_TOPIC_ARN"],
    Subject=f"dep-agent: {result['status']}",
    Message=json.dumps(result, indent=2),
)
```

Notify on `success`, `tests_failing` and `error`. Stay silent on `no_updates`.

### 6.3 What to watch

| Signal | Meaning |
|---|---|
| `llm_used: true` rate | How often updates break things |
| `fix_attempts` distribution | Clustering at 3 → prompt/tools need work |
| `status: error` with `stage` | Pipeline bug |
| `packages_updated` count | Trending up → more exposure to breakage |
| `fixed_advisories` count | Value delivered per run |
| Session duration | Creeping toward `maxLifetime` → raise it or trim tests |

---

## Security Considerations

1. **The fix agent can run any shell command.** Its input includes test output and content
   from third-party packages. A malicious package could emit steering text. The microVM is
   isolated and short-lived, but the GitHub token is inside it.
   → Scope the token to one repo. Prefer 1-hour App installation tokens. Never point this
   at a repo you don't control.

2. **Never let the agent weaken tests.** The system prompt forbids it, but prompts aren't
   enforcement. Consider a deterministic post-check: diff test files and reject if assertions
   were removed.

3. **The token must not persist.** `clone_repo` rewrites the remote URL after cloning. Push
   supplies the credential via a per-call env var. Verify in Phase 2.

4. **Credential scrubbing.** All error paths replace `token` with `***` before returning.

5. **A human merges.** Branch protection stays on. The value is a reviewed, green PR.

---

## Checklist

| Phase | Task | Status |
|---|---|---|
| 0 | Node 26 + CLI + `agentcore create` | ✅ |
| 1 | `main.py`, `pyproject.toml`, `Dockerfile` | ✅ |
| 1.4 | Image builds and runs locally | ☐ |
| 2 | Deterministic pipeline verified (`allow_fixes: false`) | ☐ |
| 2 | Fix loop verified on a deliberately broken test | ☐ |
| 3 | Deployed via `agentcore deploy`, lifetimes configured | ☐ |
| 4 | PAT in Secrets Manager, PR created on target repo | ☐ |
| 5 | Lambda + EventBridge weekly schedule | ☐ |
| 6 | Transaction Search on, SNS wired | ☐ |

---

## Estimated Cost (1 repo, weekly)

| Item | Monthly |
|---|---|
| AgentCore Runtime (~4 runs × ~$0.01) | $0.04 |
| ECR storage (~1.2 GB image) | $0.12 |
| Bedrock Claude — only when tests fail | $0.00–2.00 |
| Secrets Manager (1 secret) | $0.40 |
| CloudWatch logs + traces | ~$0.10 |
| EventBridge + Lambda | $0.00 |
| **Total** | **~$0.65–2.65** |

---

## What Changed: Rev 2 → Rev 3

| # | Change | Why |
|---|---|---|
| 1 | Region: `us-west-2` → `us-east-1` | All infra lives there |
| 2 | Model ID: `us.anthropic.claude-sonnet-4-20250514-v1:0` → `us.anthropic.claude-sonnet-4-6` | Shorter alias |
| 3 | `agentcore create` used "Bring your own code" (not Strands template) | Framework is a Python dep, not a CLI scaffold |
| 4 | Project managed by CDK (not manual zip/ECR push) | CLI generates `agentcore/cdk/` |
| 5 | Dockerfile: pinned `GH_VERSION`, `NODE_VERSION`, `PNPM_VERSION` | Reproducible builds |
| 6 | Dockerfile: pnpm via `npm install -g` (Node 26 dropped corepack) | corepack no longer available |
| 7 | Dockerfile: optional corporate proxy CA injection | Supports Netskope etc. |
| 8 | `install_deps()`: graceful fallback from `--frozen-lockfile` | Handles lockfile version mismatches |
| 9 | `_detect_pnpm_version()` / `_ensure_pnpm_version()` | Matches project's expected pnpm major |
| 10 | `run_lint()`, `run_format()`, `run_typecheck()` added | Full validation before PR |
| 11 | `snapshot_lockfile_packages()` / `diff_packages()` | Before/after package diffing |
| 12 | `extract_advisories()` | Parse CVE details from audit |
| 13 | `_build_pr_body()` | Rich PR: security table, package changes, validation results |
| 14 | `create_pr()` uses `--body-file` (not `--body`) | Handles markdown tables, long content |
| 15 | `existing_pr()` uses `--json headRefName,url` + Python filter | `--head-pattern` doesn't exist in all gh versions |
| 16 | Credential scrubbing in all error paths | Token never leaks in return payload |
| 17 | Structured logging (`[dep-agent]` prefix) throughout | Debuggable in CloudWatch |
| 18 | `update_packages()` runs `pnpm install --no-frozen-lockfile` after update | Fixes ERR_PNPM_LOCKFILE_CONFIG_MISMATCH in CI |
| 19 | Re-run lint/format/typecheck after fix agent edits | Ensures agent's code changes pass all checks |
| 20 | Removed inline `main.py` code from plan | Source of truth is now `app/depUpdateAgent/main.py` |

**Still unverified — check as you go:**
- `invoke_agent_runtime` parameter names in your boto3 version
- Whether the `opentelemetry-instrument` wrapper is needed (remove from CMD if not using OTel)
- Exact IAM permissions for the cross-region inference profile in us-east-1

---

## Next Steps After the PoC

1. **Multi-repo** — repo list in DynamoDB, one AgentCore session per repo, fan out from Lambda
2. **Update tiers** — patch/minor automatic; majors open a draft PR with migration checklist
3. **Test-integrity check** — deterministic guard that the agent didn't delete assertions
4. **AgentCore Memory** — remember how a package's breaking change was fixed last time
5. **Feedback loop** — when a human closes a PR unmerged, capture why
6. **Notifications** — SNS/Slack on success/failure with advisory summary
