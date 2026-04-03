#!/bin/bash
set -e

# 修复 home 目录权限
if [ "$(id -u)" = "0" ]; then
    chown -R node:node /home/node
fi

# 如果传入了命令，以 node 用户执行
if [ $# -gt 0 ]; then
    exec gosu node "$@"
fi

# 默认启动 openclaw
exec gosu node openclaw start
