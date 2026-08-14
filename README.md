# dep-update-agent

Autonomous dependency-update agent running on AWS Bedrock AgentCore Runtime.

Given a target repository, it clones, runs `pnpm audit`, applies patch/minor updates,
runs the test suite, and opens a pull request. When the update breaks tests, a Strands
coding agent backed by Claude diagnoses and fixes the failures before the PR is opened.

- **Deterministic pipeline** handles clone → audit → update → test → PR. No LLM tokens.
- **LLM is the escape hatch**, invoked only when tests fail. Most runs cost nothing.

See [`docs/plan.md`](docs/plan.md) for the full build and deployment plan.

## Status

Planning. Nothing implemented yet — start at Phase 0 of the plan.

## Stack

Python 3.13 · Strands Agents · Claude Sonnet (Bedrock) · AgentCore Runtime (ARM64
container) · Node 26 · pnpm · gh CLI
