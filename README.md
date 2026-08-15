# dep-update-agent

Autonomous dependency-update agent running on AWS Bedrock AgentCore Runtime.

A proof of concept showing how to build a useful agent that tackles software development
toil. The same simple principles applied here — deterministic pipeline with an LLM escape
hatch — can be adapted for dependency updates, security audits, documentation drift,
production feedback loops, and more.

## How It Works

Given a target repository, the agent:

1. Clones the repo (shallow, token scrubbed immediately)
2. Detects and matches the project's expected pnpm version
3. Runs `pnpm audit` and snapshots package versions
4. Applies patch/minor updates via `pnpm update`
5. Runs lint, format, typecheck, and tests
6. If tests fail, a Strands coding agent (Claude) diagnoses and fixes the breakage
7. Opens a pull request with a rich body: security diff, package changes, validation results

**Deterministic by default.** The pipeline runs without LLM tokens on the happy path.
Claude is invoked only when tests break — most runs cost nothing.

## Architecture

```txt
EventBridge (weekly) → Lambda → AgentCore Runtime (ARM64 container)
                                  │
                                  ├─ clone → audit → update → validate → PR  (deterministic)
                                  │
                                  └─ test failures → Strands fix agent (Claude, ≤3 attempts)
```

## Stack

Python 3.13 · Strands Agents · Claude Sonnet 4 (Bedrock) · AgentCore Runtime (ARM64
container, CDK-managed) · Node 26 · pnpm · gh CLI

## Status

Phase 0 and 1 complete — agent code, Dockerfile, and AgentCore config are implemented.
Next: local smoke test (Phase 1.4) and pipeline verification (Phase 2).

See [`docs/plan.md`](docs/plan.md) for the full step-by-step build and deployment guide.

## Quick Start

```bash
# Prerequisites: Node 26, Python 3.13, uv, Docker, AWS credentials (us-east-1)

cd dependencyUpdateAgent/app/depUpdateAgent
docker build -t dep-update-agent:local .

docker run --rm -p 8080:8080 \
  -e AWS_REGION=us-east-1 \
  -e GITHUB_SECRET_ID=dep-agent/github-pat \
  -v ~/.aws:/root/.aws:ro \
  dep-update-agent:local

# In another terminal:
curl -s localhost:8080/ping
curl -s -X POST localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"repo_url":"https://github.com/OWNER/REPO.git","allow_fixes":false}' | jq
```

## Why This Matters

This repo is a template for building agents that handle software development toil. The
pattern is simple:

1. A **deterministic pipeline** does the predictable work (clone, update, validate, PR)
2. An **LLM is the escape hatch**, called only when something unexpected happens

This same pattern applies beyond dependency updates:

- **Security audits** — scan, triage, auto-remediate known patterns
- **Documentation** — detect drift between code and docs, propose updates
- **Production feedback** — read alerts/logs, correlate with recent changes, suggest fixes
- **Migration scripts** — upgrade APIs, bump frameworks, fix deprecations

## Contributing

Test it, experiment with it, adapt it to your own toil.

- Have an idea or found a bug? Open an issue.
- Want to improve or extend it? Send a PR.
