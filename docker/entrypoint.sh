#!/bin/sh
set -e

if [ ! -f /app/config/config.yml ]; then
    cp /app/config_default.yml /app/config/config.yml
fi

exec javsp "$@"
