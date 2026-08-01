#!/bin/bash

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    printf "尚未建立正式版环境。请先运行“制作正式版.command”。\n"
    printf "按回车键关闭这个窗口……"
    read -r _
    exit 1
fi

exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/musicdownload.py"
