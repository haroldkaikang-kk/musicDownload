#!/bin/bash

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

python_minor_version() {
    "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null
}

if [ ! -x "$VENV_PYTHON" ]; then
    printf "当前 1.2.1 尚未建立独立的 Python 3.12 .venv。\n"
    printf "不会使用同层 1.2.0 .venv 运行目前版本。\n"
    printf "请先运行“制作正式版.command”建立当前版本的独立环境。\n"
    printf "按回车键关闭这个窗口……"
    read -r _
    exit 1
fi

current_version="$(python_minor_version "$VENV_PYTHON")"
if [ "$current_version" != "3.12" ]; then
    printf "当前 1.2.1 .venv 使用 Python %s，不能运行；需要 Python 3.12。\n" \
        "${current_version:-未知版本}"
    printf "不会使用同层 1.2.0 .venv 运行目前版本。\n"
    printf "请先运行“制作正式版.command”重建当前版本的独立环境。\n"
    printf "按回车键关闭这个窗口……"
    read -r _
    exit 1
fi

printf "使用当前 1.2.1 的独立 Python 3.12 .venv。\n"
exec "$VENV_PYTHON" "$SCRIPT_DIR/musicdownload.py"
