#!/bin/bash

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

APP_VERSION="1.2.1"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
PREVIOUS_VENV_DIR="$(dirname "$SCRIPT_DIR")/MusicDownload-1.2.0-Professional-macOS12-Intel/.venv"
PREVIOUS_VENV_PYTHON="$PREVIOUS_VENV_DIR/bin/python"
RELEASE_DIR="$SCRIPT_DIR/release"
DMG_STAGE="$RELEASE_DIR/dmg-stage"
DMG_PATH="$RELEASE_DIR/MusicDownload-${APP_VERSION}-macOS12-Intel.dmg"
CERTIFICATE_COMMAND="/Applications/Python 3.12/Install Certificates.command"

pause_and_exit() {
    exit_code="$1"
    shift
    printf "\n%s\n" "$*"
    printf "按回车键关闭这个窗口……"
    read -r _
    exit "$exit_code"
}

find_python312() {
    if command -v python3.12 >/dev/null 2>&1; then
        command -v python3.12
        return 0
    fi

    framework_python="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
    if [ -x "$framework_python" ]; then
        printf "%s\n" "$framework_python"
        return 0
    fi

    if command -v python3 >/dev/null 2>&1; then
        candidate="$(command -v python3)"
        candidate_version="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"
        if [ "$candidate_version" = "3.12" ]; then
            printf "%s\n" "$candidate"
            return 0
        fi
    fi

    return 1
}

python_minor_version() {
    "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null
}

find_previous_base_python312() {
    if [ -x "$PREVIOUS_VENV_PYTHON" ]; then
        previous_version="$(python_minor_version "$PREVIOUS_VENV_PYTHON")"
        if [ "$previous_version" = "3.12" ]; then
            base_python="$("$PREVIOUS_VENV_PYTHON" -c 'import sys; print(sys._base_executable)' 2>/dev/null)"
            if [ -n "$base_python" ] && [ -x "$base_python" ]; then
                base_version="$(python_minor_version "$base_python")"
                if [ "$base_version" = "3.12" ]; then
                    printf "%s\n" "$base_python"
                    return 0
                fi
            fi
            printf "同层 1.2.0 .venv 的基础 Python 无效或不是 Python 3.12，不能用于建立当前环境。\n" >&2
            return 1
        fi
        printf "同层 1.2.0 .venv 使用 Python %s，不能用于寻找基础 Python 3.12。\n" \
            "${previous_version:-未知版本}" >&2
    fi

    return 1
}

test_pypi_certificate() {
    "$VENV_PYTHON" -c '
import ssl
import urllib.request

request = urllib.request.Request(
    "https://pypi.org/simple/pip/",
    headers={"User-Agent": "MusicDownload-builder/1.2.1"},
)
with urllib.request.urlopen(
    request,
    timeout=15,
    context=ssl.create_default_context(),
) as response:
    if response.status >= 400:
        raise RuntimeError(f"PyPI HTTP {response.status}")
' >/dev/null 2>&1
}

repair_python_certificates() {
    if [ ! -f "$CERTIFICATE_COMMAND" ]; then
        return 1
    fi

    printf "\n正在运行 Python 3.12 官方证书安装器……\n"
    /bin/bash "$CERTIFICATE_COMMAND"
}

install_build_tools() {
    "$VENV_PYTHON" -m pip install \
        --disable-pip-version-check \
        --upgrade pip setuptools wheel
}

explain_certificate_recovery() {
    printf "\n无法通过 HTTPS 安全连接 PyPI。\n\n"
    printf "这表示 Python 无法验证 PyPI 的证书，不等于必须关闭 VPN。\n"
    printf "请先打开“应用程序 → Python 3.12”，双击\n"
    printf "Install Certificates.command；看到 update complete 后回到这里。\n\n"
    printf "如果证书已经安装而错误仍相同，可自行临时切换 VPN/代理线路做一次对照测试；\n"
    printf "这只是诊断选择，制作器不会关闭代理，也不会绕过 SSL 验证。\n\n"

    if [ -d "/Applications/Python 3.12" ]; then
        open "/Applications/Python 3.12" >/dev/null 2>&1 || true
    fi

    printf "完成后按回车重试；输入 q 再按回车可退出："
    read -r retry_choice
    if [ "$retry_choice" = "q" ] || [ "$retry_choice" = "Q" ]; then
        return 1
    fi
    return 0
}

clear
printf "MusicDownload %s — Mac 正式版制作器\n" "$APP_VERSION"
printf "==========================================\n\n"

if [ "$(uname -s)" != "Darwin" ]; then
    pause_and_exit 1 "本制作器只能在 macOS 上运行。"
fi

if [ "$(uname -m)" != "x86_64" ]; then
    pause_and_exit 1 "当前版本专门面向 Intel Mac（x86_64）。"
fi

printf "[1/8] 准备独立构建环境……\n"
if [ -x "$VENV_PYTHON" ] && [ "$(python_minor_version "$VENV_PYTHON")" = "3.12" ]; then
    printf "使用当前 1.2.1 的独立 Python 3.12 .venv。\n"
else
    if [ -x "$VENV_PYTHON" ]; then
        current_version="$(python_minor_version "$VENV_PYTHON")"
        printf "当前 1.2.1 .venv 使用 Python %s，需要重新建立为 Python 3.12。\n" \
            "${current_version:-未知版本}"
    else
        printf "当前 1.2.1 尚未建立独立的 Python 3.12 .venv。\n"
    fi

    if PYTHON_BIN="$(find_previous_base_python312)"; then
        printf "已从同层 1.2.0 .venv 找到基础 Python 3.12；旧环境本身不会被安装、更新或用于运行 1.2.1。\n"
    elif PYTHON_BIN="$(find_python312)"; then
        printf "已找到系统 Python 3.12，将用于建立当前 1.2.1 的独立 .venv。\n"
    else
        printf "无法从同层 1.2.0 环境或系统中找到可用于建立新环境的 Python 3.12。\n\n"
        printf "请安装 Python 3.12.10 的 macOS 64-bit universal2 installer：\n"
        printf "https://www.python.org/downloads/release/python-31210/\n"
        pause_and_exit 1 "安装 Python 3.12.10 后，再次运行本制作器。"
    fi

    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR" || pause_and_exit 1 "创建 Python 环境失败。"
    if [ ! -x "$VENV_PYTHON" ] || [ "$(python_minor_version "$VENV_PYTHON")" != "3.12" ]; then
        pause_and_exit 1 "当前 1.2.1 的 Python 3.12 虚拟环境验证失败。"
    fi
fi

printf "\n[2/8] 检查 Python 下载证书……\n"
if test_pypi_certificate; then
    printf "PyPI HTTPS 证书检查正常。\n"
else
    printf "检测到 SSL 证书问题，尝试自动修复。\n"
    repair_python_certificates || true
fi

printf "\n[3/8] 安装 macOS 12 兼容依赖……\n"
if ! install_build_tools; then
    explain_certificate_recovery ||
        pause_and_exit 1 "已取消。修复 Python 证书后可重新运行本制作器。"
    install_build_tools ||
        pause_and_exit 1 "仍无法安全连接 PyPI。请检查证书、网络或代理线路后重新运行。"
fi
"$VENV_PYTHON" -m pip install --prefer-binary -r "$SCRIPT_DIR/requirements.txt" ||
    pause_and_exit 1 "依赖安装失败，请把窗口最后 20 行发给我。"
"$VENV_PYTHON" -m pip install --no-deps "musicdl==2.13.4" ||
    pause_and_exit 1 "MusicDL 核心安装失败。"
"$VENV_PYTHON" -m pip install "pyinstaller>=6.14,<7" ||
    pause_and_exit 1 "正式版打包工具安装失败。"

printf "\n[4/8] 检查程序核心……\n"
"$VENV_PYTHON" -c "import av, mutagen, PySide6; from PySide6 import QtMultimedia; from musicdl import musicdl; from musicdl.modules import MusicClientBuilder; import musicdownload_core; missing = [source for _, source in musicdownload_core.SOURCE_DEFINITIONS if source not in MusicClientBuilder.REGISTERED_MODULES]; assert hasattr(musicdl, 'MusicClient') and not missing, missing; print('PySide6', PySide6.__version__, '/ PyAV', av.__version__, '/ 17 个 MusicDL 音源模块正常')" ||
    pause_and_exit 1 "程序核心检查失败。"
"$VENV_PYTHON" "$SCRIPT_DIR/tests/test_core.py" ||
    pause_and_exit 1 "专业功能自检失败，请把窗口最后 20 行发给我。"

printf "\n[5/8] 生成 Mac 应用图标……\n"
ICON_SOURCE="$SCRIPT_DIR/assets/MusicDownload.png"
ICONSET="$SCRIPT_DIR/build_assets/MusicDownload.iconset"
ICNS_PATH="$SCRIPT_DIR/assets/MusicDownload.icns"
rm -rf "$SCRIPT_DIR/build_assets"
mkdir -p "$ICONSET"

sips -z 16 16 "$ICON_SOURCE" --out "$ICONSET/icon_16x16.png" >/dev/null
sips -z 32 32 "$ICON_SOURCE" --out "$ICONSET/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$ICON_SOURCE" --out "$ICONSET/icon_32x32.png" >/dev/null
sips -z 64 64 "$ICON_SOURCE" --out "$ICONSET/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$ICON_SOURCE" --out "$ICONSET/icon_128x128.png" >/dev/null
sips -z 256 256 "$ICON_SOURCE" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$ICON_SOURCE" --out "$ICONSET/icon_256x256.png" >/dev/null
sips -z 512 512 "$ICON_SOURCE" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$ICON_SOURCE" --out "$ICONSET/icon_512x512.png" >/dev/null
cp "$ICON_SOURCE" "$ICONSET/icon_512x512@2x.png"
iconutil -c icns "$ICONSET" -o "$ICNS_PATH" ||
    pause_and_exit 1 "应用图标生成失败。"

printf "\n[6/8] 构建 MusicDownload.app……\n"
rm -rf "$SCRIPT_DIR/build" "$SCRIPT_DIR/dist"
"$VENV_PYTHON" -m PyInstaller --noconfirm --clean "$SCRIPT_DIR/MusicDownload.spec" ||
    pause_and_exit 1 "应用构建失败，请把窗口最后 20 行发给我。"

APP_PATH="$SCRIPT_DIR/dist/MusicDownload.app"
if [ ! -d "$APP_PATH" ]; then
    pause_and_exit 1 "没有找到构建后的 MusicDownload.app。"
fi

printf "\n[7/8] 清理并签署本机应用……\n"
xattr -cr "$APP_PATH"
codesign --force --deep --sign - "$APP_PATH" ||
    pause_and_exit 1 "本机签署失败。"
codesign --verify --deep --strict "$APP_PATH" ||
    pause_and_exit 1 "应用签署校验失败。"

printf "\n[8/8] 生成 DMG 安装盘……\n"
rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE" "$RELEASE_DIR"
ditto "$APP_PATH" "$DMG_STAGE/MusicDownload.app"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create \
    -volname "MusicDownload ${APP_VERSION}" \
    -srcfolder "$DMG_STAGE" \
    -ov \
    -format UDZO \
    "$DMG_PATH" >/dev/null || pause_and_exit 1 "DMG 安装盘生成失败。"
rm -rf "$DMG_STAGE"

printf "\n制作完成：\n%s\n" "$DMG_PATH"
open "$RELEASE_DIR"
pause_and_exit 0 "双击 DMG，将 MusicDownload 拖入 Applications 文件夹即可。"
