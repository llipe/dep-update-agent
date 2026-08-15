"""AgentCore Runtime entrypoint: autonomous dependency updater."""
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import boto3
import requests
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