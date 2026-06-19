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

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 声明卷：Docker UI (Synology/Portainer/Unraid) 会自动提示用户映射此目录
VOLUME ["/media", "/app/config"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
