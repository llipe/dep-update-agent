# AgentCore Runtime requires linux/arm64.
ARG TARGETPLATFORM=linux/arm64
FROM --platform=${TARGETPLATFORM} python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# ── Optional corporate proxy CA (Netskope etc.) ───────────────────
# ca/ always exists (contains .keep); *.crt files in it are gitignored and
# only present on machines behind a TLS-intercepting proxy. In CodeBuild the
# directory is effectively empty and this step is a no-op.
COPY ca/ /tmp/ca/
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && cp /tmp/ca/*.crt /usr/local/share/ca-certificates/ 2>/dev/null || true \
    && update-ca-certificates \
    && rm -rf /tmp/ca /var/lib/apt/lists/*

# ── System deps: git, curl, gh CLI ────────────────────────────────
ENV GH_VERSION=2.97.0
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git jq xz-utils libatomic1 \
    && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.deb" \
        -o /tmp/gh.deb \
    && dpkg -i /tmp/gh.deb && rm /tmp/gh.deb \
    && rm -rf /var/lib/apt/lists/*

# ── Node 26 via official binary tarball ────────────────────────────
ENV NODE_VERSION=26.6.0
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz" \
        -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && node --version && npm --version

# ── pnpm (Node 26 no longer ships corepack) ───────────────────────
ENV PNPM_VERSION=11.21.0
RUN npm install -g "pnpm@${PNPM_VERSION}" && pnpm --version

# ── Python deps ───────────────────────────────────────────────────
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv export --no-dev --format requirements-txt \
    > requirements.txt && pip install --no-cache-dir -r requirements.txt

COPY main.py ./

# AgentCore health-checks /ping and invokes /invocations on 8080.
EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "main.py"]