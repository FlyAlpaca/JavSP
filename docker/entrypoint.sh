#!/bin/sh
set -e

# 运行时传入的 PATH 可能覆盖镜像 ENV，启动前恢复应用和 llama.cpp 命令目录。
export PATH="/app/.venv/bin:/opt/llama:$PATH"

if [ ! -f /app/config/config.yml ]; then
    cp /app/config_default.yml /app/config/config.yml
fi

exec javsp "$@"
