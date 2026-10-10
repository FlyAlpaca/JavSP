FROM debian:trixie-slim AS llama-binary
ARG TARGETARCH
ARG LLAMA_CPP_VERSION=b11541
ARG LLAMA_CPP_SHA256=36ca310be4405acb7ba59b32dbdce32bb9cd7b1ef95358997b7eaed2c6f0188b
RUN set -eu; \
    test "$TARGETARCH" = amd64; \
    apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*; \
    curl -fsSL --retry 3 \
      "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_CPP_VERSION}/llama-${LLAMA_CPP_VERSION}-bin-ubuntu-x64.tar.gz" \
      -o /tmp/llama.tar.gz; \
    echo "$LLAMA_CPP_SHA256  /tmp/llama.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/llama; \
    tar -xzf /tmp/llama.tar.gz -C /opt/llama --strip-components=1; \
    test -x /opt/llama/llama-server

FROM python:3.14-slim AS builder
WORKDIR /app

# uv 由 Astral 官方镜像复制
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# 只复制构建所需文件（利用 .dockerignore 过滤）
COPY . .

# 版本号由 CI build-arg 传入，不依赖 .git
ARG VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=$VERSION

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# 只安装运行时依赖和项目本身（可编辑模式，不装 dev）
RUN uv sync --no-dev

FROM python:3.14-slim AS runner
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgomp1 libssl3t64 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=llama-binary /opt/llama/ /opt/llama/
COPY --from=llama-binary /opt/llama/LICENSE /usr/share/doc/llama.cpp/LICENSE

# 复制虚拟环境（含 javsp 的可编辑安装链接）
COPY --from=builder /app/.venv /app/.venv
# 可编辑安装通过 .pth 指向源目录，必须保留 javsp 源码
COPY --from=builder /app/javsp /app/javsp
# resource_path() 在可编辑安装下解析到 /app/
# config_default.yml 放在 VOLUME 外作为种子，entrypoint 首次运行时拷贝为 config.yml
COPY --from=builder /app/config_default.yml /app/config_default.yml
COPY --from=builder /app/data /app/data
COPY --from=builder /app/image /app/image
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

ENV PATH="/app/.venv/bin:/opt/llama:${PATH}" \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 声明媒体和配置持久化目录
VOLUME ["/media", "/app/config", "/app/models"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
