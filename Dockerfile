# AgentCore Runtime requires linux/arm64.
FROM --platform=linux/arm64 python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    NODE_MAJOR=26

# -- System deps: git, curl, gh CLI --
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git jq \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] \
https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# -- Node 26 via NodeSource --
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

# -- pnpm via corepack --
RUN corepack enable && corepack prepare pnpm@latest --activate && pnpm --version

# -- Python deps --
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv export --no-dev --format requirements-txt \
    > requirements.txt && pip install --no-cache-dir -r requirements.txt

COPY main.py ./

# AgentCore health-checks /ping and invokes /invocations on 8080.
EXPOSE 8080
CMD ["python", "main.py"]
