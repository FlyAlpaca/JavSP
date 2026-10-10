FROM debian:trixie-slim AS llama-builder
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates cmake g++ git make \
    && rm -rf /var/lib/apt/lists/*
ARG LLAMA_CPP_REF=781dbc5ac98921dbdb5e5b2ec5b7a50960e937d4
RUN git clone --filter=blob:none https://github.com/ggml-org/llama.cpp.git /src/llama.cpp \
    && cd /src/llama.cpp && git checkout "$LLAMA_CPP_REF" \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF -DBUILD_SHARED_LIBS=OFF -DLLAMA_CURL=OFF \
    && cmake --build build --target llama-server -j 2

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
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=llama-builder /src/llama.cpp/build/bin/ /opt/llama/
COPY --from=llama-builder /src/llama.cpp/LICENSE /usr/share/doc/llama.cpp/LICENSE

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
