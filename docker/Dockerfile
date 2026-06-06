FROM python:3.14-slim AS builder

WORKDIR /app

# uv 由 Astral 官方镜像复制得到；与官方 Python 镜像搭配时无需额外依赖
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt update && apt install -y git

# 完整复制源码（含 .git）以便 hatch-vcs 读取 tag 计算版本
COPY . .

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1
RUN uv sync --extra dev

FROM python:3.14-slim AS runner

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/data /app/data
COPY --from=builder /app/image /app/image
COPY --from=builder /app/config.yml /app/config.yml
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:${PATH}"
ENTRYPOINT ["/app/.venv/bin/javsp"]
CMD ["-i", "/video"]
