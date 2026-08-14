# Plan: Dependency Update Agent on AWS AgentCore

> Vibecoding guide — follow step by step, deploy to AWS, connect to GitHub.
> **Revision 2** — corrects a blocking flaw in rev 1 (see "Review Notes" at the end).

---

## ⚠️ The One Thing That Changed From Rev 1

Rev 1 used **direct code deployment** (zip of Python code). That cannot work here.

AgentCore Runtime's direct-code path gives you a **managed Python runtime only** — it has no
`git`, no `node`, no `pnpm`, no `gh` binaries. Every `subprocess.run(["pnpm", ...])` call in the
pipeline would die with `FileNotFoundError`.

**This agent must use container deployment.** You build an ARM64 image with Node 26 + pnpm +
git + gh + Python, push it to ECR, and point AgentCore Runtime at it. AWS documents this
explicitly: *"Bring your own container image for custom environments with additional
dependencies."*

Two hard constraints that follow:
- **ARM64 only.** AgentCore requires `linux/arm64` for all deployed agents.
- **The container must expose `/invocations` (POST) and `/ping` (GET).** The
  `BedrockAgentCoreApp` SDK does this for you, so your Python code barely changes.

---

## Architecture

```
EventBridge Scheduler (weekly cron)
        │
        ▼
Lambda trigger ── InvokeAgentRuntime ──┐
                                       ▼
                    AgentCore Runtime (ARM64 microVM, custom container)
                    ┌──────────────────────────────────────────────┐
                    │ Node 26 · pnpm · git · gh · Python 3.13      │
                    ├──────────────────────────────────────────────┤
                    │ 1. Fetch GitHub token (Secrets Manager)      │
                    │ 2. git clone                                 │
                    │ 3. pnpm install --frozen-lockfile            │
                    │ 4. pnpm audit --json                         │
                    │ 5. pnpm update  →  pnpm install              │
                    │ 6. pnpm test                                 │
                    │        │                                     │
                    │   PASS │                          FAIL       │
                    │        ▼                            ▼        │
                    │   create PR              Strands coding agent│
                    │  (deterministic)      shell/read/write/grep  │
                    │                       iterate ≤3 → PR or bail│
                    └──────────────────────────────────────────────┘
                                       │
                                       ▼
                             Return {status, pr_url}
```

**Stack**: Python 3.13 · Strands Agents · Claude Sonnet (Bedrock) · AgentCore Runtime
(container) · Node 26 · pnpm · gh CLI

**Cost model**: $0 LLM tokens on the happy path. Claude is invoked only when tests fail.

---

## Prerequisites

- [ ] AWS account, credentials configured (`aws configure` or `aws sso login`)
- [ ] **Node.js 26** (for the AgentCore CLI — needs 20+, we standardise on 26)
- [ ] Python 3.13+ (via `uv`)
- [ ] `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [ ] Docker with buildx (for ARM64 builds)
- [ ] `gh` CLI authenticated locally: `gh auth login`
- [ ] Claude Sonnet enabled in Bedrock → Model access (pick one region and stay in it)
- [ ] **A target repo to run against — not yet chosen.** Referred to throughout as
      `<OWNER>/<TARGET_REPO>`. It needs a `pnpm-lock.yaml` and a `test` script in
      `package.json`. A scratch repo you own is the right call for the first runs: the agent
      pushes branches and opens PRs, so you don't want to aim it at anything you care about
      until Phase 2 passes.

> **Apple Silicon note**: you're on arm64 natively, so `docker build` produces the right
> architecture with no emulation. On an x86 machine you'd need `--platform linux/arm64` and
> QEMU, which is slow — or build via CodeBuild on ARM.

---

## Phase 0: Local Environment

### 0.1 Pin Node 26

```bash
# via nvm
nvm install 26 && nvm use 26 && nvm alias default 26

# or via fnm
fnm install 26 && fnm use 26 && fnm default 26

node --version   # expect v26.x
```

### 0.2 Install the AgentCore CLI

```bash
npm install -g @aws/agentcore
agentcore --version
```

### 0.3 Scaffold the project

```bash
mkdir dep-update-agent && cd dep-update-agent
uv init --python 3.13
uv add bedrock-agentcore strands-agents boto3 "pyjwt[crypto]" requests
```

Pin Node for the project too, so the container and your machine agree:

```bash
echo "26" > .nvmrc
```

### 0.4 Let the CLI generate its own config

```bash
agentcore create
```

Pick **Strands Agents** as the framework. This writes `agentcore/agentcore.json` and
`agentcore/aws-targets.json`.

> Do **not** hand-write `agentcore.json` from a blog post or from rev 1 of this plan — the
> schema is CLI-owned and moves between versions. Generate it, then edit fields you
> understand. You will need to switch the deployment type to **container** and raise the
> session lifetime (see Phase 3.2).

---

## Phase 1: The Agent Code

### 1.1 `main.py`

```python
"""AgentCore Runtime entrypoint: autonomous dependency updater."""
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool

# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────

MODEL_ID = os.environ.get(
    "MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"
)
SECRET_ID = os.environ.get("GITHUB_SECRET_ID", "dep-agent/github-pat")
TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "600"))

# Set per-invocation. Tools read it instead of taking a path argument,
# so the model cannot wander outside the checkout.
_workspace: str | None = None


def _ws() -> str:
    if _workspace is None:
        raise RuntimeError("workspace not initialised")
    return _workspace


def _safe_path(rel: str) -> str:
    """Resolve a caller-supplied path and refuse anything outside the workspace."""
    root = os.path.realpath(_ws())
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f"path escapes workspace: {rel}")
    return target


# ─────────────────────────────────────────────────────────────────
# Tools for the fix agent
# ─────────────────────────────────────────────────────────────────

@tool
def shell(command: str) -> str:
    """Run a shell command inside the repository checkout.

    Args:
        command: The shell command to run, e.g. 'pnpm test' or 'pnpm why react'.
    """
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True,
        cwd=_ws(), timeout=180,
    )
    out = f"[exit {result.returncode}]\n"
    if result.stdout:
        out += result.stdout[-3000:]
    if result.stderr:
        out += "\n--- stderr ---\n" + result.stderr[-1500:]
    return out


@tool
def read_file(path: str) -> str:
    """Read a file from the repository, relative to the repo root.

    Args:
        path: Repo-relative file path, e.g. 'src/index.ts'.
    """
    with open(_safe_path(path)) as f:
        content = f.read()
    return content[:8000] + "\n... [truncated]" if len(content) > 8000 else content


@tool
def write_file(path: str, content: str) -> str:
    """Overwrite a file in the repository with new content.

    Args:
        path: Repo-relative file path.
        content: The complete new file contents.
    """
    full = _safe_path(path)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return f"wrote {len(content)} bytes to {path}"


@tool
def find_files(pattern: str) -> str:
    """Find files by name pattern, skipping node_modules.

    Args:
        pattern: A filename glob, e.g. '*.test.ts'.
    """
    result = subprocess.run(
        ["find", ".", "-name", pattern, "-not", "-path", "*/node_modules/*"],
        capture_output=True, text=True, cwd=_ws(), timeout=30,
    )
    lines = result.stdout.splitlines()[:30]
    return "\n".join(lines) or "no files found"


@tool
def grep_code(pattern: str, file_glob: str = "*.ts") -> str:
    """Search source files for a pattern. Use to locate a deprecated API call site.

    Args:
        pattern: The literal text or regex to search for.
        file_glob: Restrict to files matching this glob. Defaults to '*.ts'.
    """
    result = subprocess.run(
        ["grep", "-rn", "--include", file_glob,
         "--exclude-dir", "node_modules", pattern, "."],
        capture_output=True, text=True, cwd=_ws(), timeout=60,
    )
    lines = result.stdout.splitlines()[:20]
    return "\n".join(lines) or "no matches"


# ─────────────────────────────────────────────────────────────────
# Deterministic pipeline (no LLM, no tokens)
# ─────────────────────────────────────────────────────────────────

def get_github_token() -> str:
    """Read the GitHub token from Secrets Manager."""
    sm = boto3.client("secretsmanager")
    raw = sm.get_secret_value(SecretId=SECRET_ID)["SecretString"]
    data = json.loads(raw)
    if "token" in data:                      # PAT
        return data["token"]
    return _installation_token(data)         # GitHub App


def _installation_token(secret: dict) -> str:
    """Exchange GitHub App credentials for a short-lived installation token."""
    import jwt

    now = int(time.time())
    assertion = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": secret["app_id"]},
        secret["private_key"], algorithm="RS256",
    )
    resp = requests.post(
        f"https://api.github.com/app/installations/"
        f"{secret['installation_id']}/access_tokens",
        headers={"Authorization": f"Bearer {assertion}",
                 "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def _run(cmd: list[str], cwd: str, timeout: int = 300, check: bool = True):
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        timeout=timeout, check=check,
    )


def clone_repo(repo_url: str, workspace: str, token: str) -> None:
    """Clone with a token, then scrub the token out of .git/config."""
    authed = repo_url.replace("https://", f"https://x-access-token:{token}@")
    _run(["git", "clone", "--depth", "1", authed, workspace], cwd="/tmp")
    # Never leave the credential on disk in the remote URL.
    _run(["git", "remote", "set-url", "origin", repo_url], cwd=workspace)
    # A container has no git identity; commit would fail without this.
    _run(["git", "config", "user.name", "dep-update-agent"], cwd=workspace)
    _run(["git", "config", "user.email",
          "dep-update-agent@users.noreply.github.com"], cwd=workspace)


def install_deps(workspace: str, frozen: bool = True) -> None:
    """Install node_modules. Required before audit and before tests."""
    cmd = ["pnpm", "install"] + (["--frozen-lockfile"] if frozen else [])
    _run(cmd, cwd=workspace, timeout=600)


def run_audit(workspace: str) -> dict:
    """pnpm audit exits non-zero when vulnerabilities exist, so check=False."""
    result = _run(["pnpm", "audit", "--json"], cwd=workspace, check=False)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"parse_failed": True, "raw": result.stdout[:2000]}


def count_vulns(audit: dict) -> int:
    vulns = audit.get("metadata", {}).get("vulnerabilities", {})
    return sum(v for v in vulns.values() if isinstance(v, int))


def update_packages(workspace: str) -> str:
    """pnpm update: patch + minor within existing semver ranges. No majors."""
    result = _run(["pnpm", "update"], cwd=workspace, timeout=600, check=False)
    return (result.stdout + result.stderr)[:2000]


def has_changes(workspace: str) -> bool:
    result = _run(["git", "status", "--porcelain"], cwd=workspace)
    return bool(result.stdout.strip())


def run_tests(workspace: str) -> tuple[int, str]:
    try:
        result = _run(["pnpm", "test"], cwd=workspace,
                      timeout=TEST_TIMEOUT, check=False)
        return result.returncode, result.stdout + "\n" + result.stderr
    except subprocess.TimeoutExpired:
        return 124, f"test suite exceeded {TEST_TIMEOUT}s and was killed"


def default_branch(workspace: str) -> str:
    result = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd=workspace)
    return result.stdout.strip() or "main"


def existing_pr(workspace: str, env: dict) -> str | None:
    """Idempotency: don't open a second PR if one of ours is already open."""
    result = subprocess.run(
        ["gh", "pr", "list", "--state", "open",
         "--head-pattern", "deps/update-", "--json", "url"],
        cwd=workspace, capture_output=True, text=True, env=env, check=False,
    )
    if result.returncode != 0:
        return None
    try:
        prs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return prs[0]["url"] if prs else None


def create_pr(workspace: str, token: str, base: str,
              body: str) -> str:
    """Branch, commit, push, open PR. Returns the PR URL."""
    env = {**os.environ, "GH_TOKEN": token}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"deps/update-{stamp}"

    _run(["git", "checkout", "-b", branch], cwd=workspace)
    _run(["git", "add", "-A"], cwd=workspace)
    _run(["git", "commit", "-m",
          "chore(deps): automated dependency update"], cwd=workspace)

    # Push needs the credential; supply it for this call only.
    subprocess.run(
        ["git", "push", "origin", branch],
        cwd=workspace, check=True, capture_output=True, text=True,
        env={**env, "GIT_ASKPASS": "true",
             "GIT_CONFIG_COUNT": "1",
             "GIT_CONFIG_KEY_0": "credential.helper",
             "GIT_CONFIG_VALUE_0":
                 f"!f() {{ echo username=x-access-token; echo password={token}; }}; f"},
    )

    result = subprocess.run(
        ["gh", "pr", "create",
         "--title", "chore(deps): automated dependency update",
         "--body", body,
         "--base", base, "--head", branch],
        cwd=workspace, capture_output=True, text=True, env=env, check=True,
    )
    return result.stdout.strip()


# ─────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────

app = BedrockAgentCoreApp()


@app.entrypoint
def dep_update(payload, context):
    global _workspace

    repo_url = payload["repo_url"]
    max_attempts = int(payload.get("max_fix_attempts", 3))
    allow_fixes = bool(payload.get("allow_fixes", True))

    _workspace = tempfile.mkdtemp(prefix="dep-agent-", dir="/tmp")
    token = get_github_token()

    try:
        clone_repo(repo_url, _workspace, token)
        base = default_branch(_workspace)
        install_deps(_workspace, frozen=True)

        audit = run_audit(_workspace)
        vuln_count = count_vulns(audit)

        update_packages(_workspace)
        if not has_changes(_workspace):
            return {"status": "no_updates", "vulnerabilities": vuln_count}

        # Lockfile moved, so node_modules must be re-resolved before testing.
        install_deps(_workspace, frozen=False)

        exit_code, test_output = run_tests(_workspace)
        attempts = 0

        if exit_code != 0 and allow_fixes:
            fix_agent = Agent(
                model=MODEL_ID,
                tools=[shell, read_file, write_file, find_files, grep_code],
                system_prompt=(
                    "You are a senior engineer fixing a test suite that broke after "
                    "`pnpm update` bumped dependencies. You have tools to read, search "
                    "and edit the repo and to run commands.\n"
                    "Method: read the failure, locate the call site, work out what the "
                    "new package version changed, apply the smallest fix, then run "
                    "`pnpm test` to verify.\n"
                    "Constraints: change only what is needed to make tests pass. Never "
                    "delete, skip or weaken a test to make it green. Never edit "
                    "package.json versions to roll a dependency back — the point of "
                    "this run is to land the update."
                ),
            )
            while exit_code != 0 and attempts < max_attempts:
                attempts += 1
                fix_agent(
                    f"Attempt {attempts} of {max_attempts}.\n\n"
                    f"Test output (tail):\n{test_output[-4000:]}\n\n"
                    "Diagnose and fix. Then run `pnpm test`."
                )
                exit_code, test_output = run_tests(_workspace)

        if exit_code != 0:
            return {
                "status": "tests_failing",
                "vulnerabilities": vuln_count,
                "fix_attempts": attempts,
                "test_output": test_output[-2000:],
                "llm_used": attempts > 0,
            }

        env = {**os.environ, "GH_TOKEN": token}
        already = existing_pr(_workspace, env)
        if already:
            return {"status": "pr_already_open", "pr_url": already}

        body = (
            "Automated dependency update by `dep-update-agent`.\n\n"
            f"- Vulnerabilities found before update: **{vuln_count}**\n"
            f"- Updated via `pnpm update` (patch/minor, no majors)\n"
            f"- Test suite: passing"
            + (f" after {attempts} automated fix attempt(s)" if attempts else "")
            + "\n\nReview the diff before merging."
        )
        pr_url = create_pr(_workspace, token, base, body)

        return {
            "status": "success",
            "pr_url": pr_url,
            "vulnerabilities": vuln_count,
            "fix_attempts": attempts,
            "llm_used": attempts > 0,
        }

    except subprocess.CalledProcessError as e:
        return {
            "status": "error",
            "stage": " ".join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd),
            "stderr": (e.stderr or "")[-1500:],
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


app.run()
```

Add the missing top-level import (the App-token path needs it):

```python
import requests  # noqa — used by _installation_token
```

### 1.2 `pyproject.toml`

```toml
[project]
name = "dep-update-agent"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "bedrock-agentcore",
    "strands-agents",
    "boto3",
    "pyjwt[crypto]",   # [crypto] is required for RS256 — plain pyjwt fails
    "requests",
]
```

> Versions deliberately unpinned here because `bedrock-agentcore` and `strands-agents` are
> moving fast. Run `uv lock`, commit `uv.lock`, and pin from what resolves.

### 1.3 `Dockerfile` (ARM64, Node 26)

This is the piece rev 1 was missing entirely.

```dockerfile
# AgentCore Runtime requires linux/arm64.
FROM --platform=linux/arm64 python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    NODE_MAJOR=26

# ── System deps: git, curl, gh CLI ────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git jq \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] \
https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# ── Node 26 via NodeSource ────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

# ── pnpm via corepack ─────────────────────────────────────────────
RUN corepack enable && corepack prepare pnpm@latest --activate && pnpm --version

# ── Python deps ───────────────────────────────────────────────────
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv export --no-dev --format requirements-txt \
    > requirements.txt && pip install --no-cache-dir -r requirements.txt

COPY main.py ./

# AgentCore health-checks /ping and invokes /invocations on 8080.
EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "main.py"]
```

> If you skip the OTel dependency, change the CMD to `["python", "main.py"]`.

### 1.4 Local smoke test of the image

```bash
docker build -t dep-update-agent:local .

docker run --rm -p 8080:8080 \
  -e AWS_REGION=us-west-2 \
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

Start with `"allow_fixes": false` so the first run costs zero LLM tokens and you're only
validating the deterministic pipeline.

---

## Phase 2: Verify the Pipeline Before Spending on the LLM

Work through these in order. Each one isolates a different failure mode.

- [ ] `/ping` returns 200 — the SDK server is up
- [ ] Token is read from Secrets Manager (create the secret first, Phase 4)
- [ ] `git clone` succeeds and the token is **not** in `.git/config` afterwards
      (`git -C /tmp/dep-agent-*/ remote -v`)
- [ ] `pnpm install --frozen-lockfile` succeeds — fails loudly if the lockfile is stale
- [ ] `pnpm audit --json` parses (returns non-zero exit with vulns; that's expected)
- [ ] `pnpm update` produces a lockfile diff (`has_changes` is true)
- [ ] `pnpm test` runs. If the repo has no `test` script, `pnpm test` errors —
      add `"test": "echo ok"` to `package.json` on a scratch branch to exercise the flow
- [ ] `git commit` succeeds — proves the container git identity is configured
- [ ] PR is created and its URL comes back
- [ ] Run it **twice**: the second run must return `pr_already_open`, not a duplicate PR

Only once all of the above pass, flip `allow_fixes` to `true` and deliberately break a test
to exercise the Strands agent.

---

## Phase 3: Deploy to AWS

### 3.1 Push the image to ECR

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-2
REPO=dep-update-agent

aws ecr create-repository --repository-name $REPO --region $REGION

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

docker build --platform linux/arm64 \
  -t $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest .

docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest
```

### 3.2 Session lifetime — set this before you deploy

Defaults will kill a real run. A dependency update on a non-trivial repo is
`pnpm install` + `pnpm update` + `pnpm install` + tests, and the fix loop adds more test
runs on top.

| Setting | Default | Set to | Why |
|---|---|---|---|
| `idleRuntimeSessionTimeout` | 15 min | 300 s | Reap the microVM promptly after the run ends |
| `maxLifetime` | — | 3600 s | Ceiling for a slow repo plus 3 fix attempts |
| `TEST_TIMEOUT` (env) | 600 s | tune per repo | Must be well under `maxLifetime` |

Configure these through `agentcore/agentcore.json` (or the `lifecycleConfiguration` block if
you're calling `create_agent_runtime` via boto3).

### 3.3 IAM for the runtime execution role

```
bedrock:InvokeModel                    on the Claude model AND the inference profile
bedrock:InvokeModelWithResponseStream   ditto
secretsmanager:GetSecretValue           on dep-agent/*
logs:CreateLogStream, logs:PutLogEvents on the agent's log group
xray:PutTraceSegments                   if OTel is enabled
```

> **Gotcha**: `us.anthropic.claude-sonnet-4-...` is a *cross-region inference profile*, not a
> plain model ID. The policy needs `bedrock:InvokeModel` on both the profile ARN **and** the
> underlying foundation-model ARNs in every region the profile can route to. If you get
> `AccessDeniedException` with the model enabled, this is almost always why. Using the plain
> regional model ID sidesteps it at the cost of throughput.

Your **own** (deploying) principal separately needs `bedrock-agentcore:*`, `ecr:*` on the
repo, and `iam:PassRole` for the execution role.

### 3.4 Deploy and invoke

```bash
agentcore deploy
agentcore status
agentcore invoke '{"repo_url":"https://github.com/<OWNER>/<TARGET_REPO>.git","allow_fixes":false}'
```

---

## Phase 4: GitHub Connection

### 4.1 Start with a fine-grained PAT (do this first)

Fewer moving parts, and it lets you validate the whole pipeline before adding JWT signing.

Create at https://github.com/settings/personal-access-tokens/new
- Repository access: **only** the single repo you picked as the target
- Permissions: Contents **Read/Write**, Pull requests **Read/Write**, Metadata **Read**
- Expiry: 90 days (put a calendar reminder — an expired token surfaces as a confusing
  `git push` auth failure)

```bash
aws secretsmanager create-secret \
  --name dep-agent/github-pat \
  --secret-string '{"token":"github_pat_..."}'
```

### 4.2 Move to a GitHub App when you go multi-repo

A PAT is tied to you personally and grants the union of everything it can reach. A GitHub App
issues **installation tokens that expire in one hour** and are scoped per installation — the
right shape once more than one repo is in play.

1. https://github.com/settings/apps/new
2. Permissions: Contents R/W, Pull requests R/W, Metadata R
3. Install it on the target repos, note the **installation ID** from the install URL
4. Generate a private key (`.pem`)

```bash
aws secretsmanager create-secret \
  --name dep-agent/github-app \
  --secret-string "$(jq -n \
    --arg app_id "123456" \
    --arg inst_id "78901234" \
    --arg key "$(cat dep-update-agent.private-key.pem)" \
    '{app_id:$app_id, installation_id:$inst_id, private_key:$key}')"
```

Then point the agent at it: `GITHUB_SECRET_ID=dep-agent/github-app`. The `get_github_token`
function already branches on which shape the secret has.

### 4.3 Branch protection interaction

If `main` is protected on the target repo, the agent can push its `deps/*` branch
and open the PR, but **cannot merge**. That's the desired end state — a human reviews and
merges. Just don't be surprised when the agent can't self-merge.

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

> Rev 1 passed `agentRuntimeId=`. The data-plane call takes **`agentRuntimeArn`**. Confirm
> against `aws bedrock-agentcore invoke-agent-runtime help` for your CLI/boto3 version before
> trusting either — this API surface is young and parameter names have shifted.

A synchronous invoke will hold the Lambda open for the whole run, so give the Lambda a
15-minute timeout, or invoke asynchronously and let the agent report via SNS (Phase 6).

### 5.2 EventBridge rule

```bash
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

> The `add-permission` step is easy to forget and the failure is silent — the rule fires and
> nothing happens.

---

## Phase 6: Observability & Notifications

### 6.1 Enable Transaction Search

AgentCore Observability needs CloudWatch Transaction Search turned on once per account
before traces appear. Console → CloudWatch → Application Signals → Transaction Search.

### 6.2 SNS notification

```python
sns = boto3.client("sns")
sns.publish(
    TopicArn=os.environ["NOTIFY_TOPIC_ARN"],
    Subject=f"dep-agent: {result['status']}",
    Message=json.dumps(result, indent=2),
)
```

Notify on `success`, `tests_failing` and `error`. Stay silent on `no_updates` — a weekly
"nothing happened" email trains you to ignore the channel.

### 6.3 What to watch

| Signal | Meaning |
|---|---|
| `llm_used: true` rate | How often updates actually break things |
| `fix_attempts` distribution | Clustering at 3 means the prompt or tools need work |
| `status: error` with a `stage` | Pipeline bug, not a repo problem |
| Session duration | Creeping toward `maxLifetime` means raise it or trim tests |

---

## Security Considerations

Worth being explicit about, because this agent has real credentials and an arbitrary-command
tool in the same process.

1. **The fix agent can run any shell command.** Its input includes test output and,
   indirectly, content from freshly-updated third-party packages. A malicious package could
   emit text crafted to steer the agent. The microVM is isolated and short-lived, but the
   GitHub token is inside it.
   → Mitigations: scope the token to one repo, prefer 1-hour App installation tokens over
   long-lived PATs, and never point this agent at a repo you don't control.

2. **Never let the agent weaken tests.** The system prompt forbids deleting or skipping
   tests, but a prompt is not an enforcement mechanism. Consider a deterministic post-check:
   diff the test files and reject the PR if assertions were removed.

3. **The token must not persist.** `clone_repo` rewrites the remote after cloning and the
   push supplies the credential via a per-call env var, so it never lands in `.git/config`.
   Verify this yourself in Phase 2 — it's the kind of thing that silently regresses.

4. **A human merges.** Keep branch protection on. The value here is a reviewed PR that's
   already green, not an unattended commit to `main`.

---

## Checklist

| Phase | Task | Status |
|---|---|---|
| 0 | Node 26 + CLI + `agentcore create` | ☐ |
| 1 | `main.py`, `pyproject.toml`, `Dockerfile` | ☐ |
| 1.4 | Image builds and runs locally | ☐ |
| 2 | Deterministic pipeline verified end to end (`allow_fixes: false`) | ☐ |
| 2 | Fix loop verified on a deliberately broken test | ☐ |
| 3 | Image in ECR, runtime deployed, lifetimes configured | ☐ |
| 4 | PAT in Secrets Manager, PR created on the target repo | ☐ |
| 5 | Lambda + EventBridge weekly schedule | ☐ |
| 6 | Transaction Search on, SNS notifications wired | ☐ |

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

The image is the fixed cost floor here (Node + gh + Python is ~1.2 GB) and it's still cents.
The variable cost is entirely "how often do updates break the build".

---

## Review Notes (rev 1 → rev 2)

What changed and why, so the reasoning survives:

| # | Issue in rev 1 | Severity | Fix |
|---|---|---|---|
| 1 | Direct code (zip) deployment — no `git`/`node`/`pnpm`/`gh` in a managed Python runtime | **Blocker** | Container deployment, ARM64, Dockerfile with Node 26 |
| 2 | `from strands.tools import tool` | **Blocker** | `from strands import tool` (verified against Strands docs) |
| 3 | No `pnpm install` before audit/test — `node_modules` absent | **Blocker** | `install_deps()` before audit, and again after `pnpm update` |
| 4 | `gh` never authenticated | **Blocker** | `GH_TOKEN` in the env for every `gh` call |
| 5 | `git commit` with no user identity — fails in a fresh container | **Blocker** | `git config user.name/user.email` after clone |
| 6 | Token baked into `origin` URL, left in `.git/config` | High | `git remote set-url` after clone; credential passed per-push |
| 7 | Branch named by date — second run same day collides | High | UTC timestamp to the second |
| 8 | No idempotency — repeat runs open duplicate PRs | High | `existing_pr()` check before creating |
| 9 | `pyjwt`/`requests`/`boto3` missing from dependencies | High | Added; `pyjwt[crypto]` for RS256 |
| 10 | Hand-written `agentcore.json` schema | Medium | Generate via `agentcore create` |
| 11 | `os.makedirs(dirname)` crashes on a root-level file (`dirname` is `""`) | Medium | Guard before `makedirs` |
| 12 | Tools took absolute paths — model could read outside the checkout | Medium | `_safe_path()` confines every path to the workspace |
| 13 | `agentRuntimeId=` in the Lambda | Medium | `agentRuntimeArn=`, flagged to re-verify |
| 14 | Session/idle timeouts never set — 15-min default kills real runs | Medium | Explicit `maxLifetime` / `idleRuntimeSessionTimeout` table |
| 15 | Cross-region inference profile IAM not mentioned | Medium | Called out as the usual `AccessDenied` cause |
| 16 | Missing `lambda add-permission` for EventBridge | Medium | Added — silent failure otherwise |
| 17 | No commit when `pnpm update` was a no-op | Low | `has_changes()` → early `no_updates` return |
| 18 | Test timeout could exceed session lifetime | Low | `TEST_TIMEOUT` env, documented relationship |
| 19 | Prompt injection / test-weakening risk unaddressed | — | Security Considerations section |

**Still unverified — check as you go.** These move fast enough that this document should not
be trusted over the CLI's own output:
- exact `agentcore.json` schema for container deployment and lifecycle config
- `invoke_agent_runtime` parameter names in your boto3 version
- whether `gh pr list --head-pattern` exists in the `gh` version the image installs
  (fall back to `gh pr list --json headRefName,url` and filter in Python if not)

---

## Next Steps After the PoC

1. **Multi-repo** — repo list in DynamoDB, one AgentCore session per repo, fan out from the Lambda
2. **Update tiers** — patch/minor automatic; majors open a draft PR with a migration checklist
3. **Richer PRs** — before/after audit diff, changelog links, blast-radius summary
4. **Test-integrity check** — deterministic guard that the agent didn't delete assertions
5. **AgentCore Memory** — remember how a given package's breaking change was fixed last time
6. **Feedback loop** — when a human closes a PR unmerged, capture why
