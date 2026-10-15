# ── Stage 1: build ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.3 /uv /usr/local/bin/uv

# Build at the same path the runtime uses: venv console scripts hard-code it in their shebang.
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first so this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the package itself, installed non-editable into the venv.
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash hpc
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Runtime inputs, all mounted by docker-compose.yml (never baked into the image):
#   /run/secrets/hpc_key        SSH private key (read-only)
#   /run/secrets/known_hosts    known_hosts with the cluster's host key (read-only)
#   /app/cluster/               cluster.toml + docs/ (read-only)
ENV HPC_KEY_PATH=/run/secrets/hpc_key \
    HPC_KNOWN_HOSTS=/run/secrets/known_hosts \
    HPC_CLUSTER_CONFIG=/app/cluster/cluster.toml

USER hpc

# MCP over stdio — no ports.
ENTRYPOINT ["slurm-mcp"]
