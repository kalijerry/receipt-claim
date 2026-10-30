#!/bin/bash
# 双击启动报销助手。首次运行会自动安装 uv、创建虚拟环境、安装依赖并编译识别工具（需要联网，只此一次）。
# 在 DMG 里直接双击也可以：那里是只读的，会先复制到 ~/报销助手，再从那里运行。
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"

if [ ! -w "$HERE" ]; then
  DEST="$HOME/报销助手"
  echo "正在安装到 $DEST …"
  mkdir -p "$DEST" &&
    rsync -a --delete --exclude='/output' --exclude='/app/.venv' --exclude='/app/ocr/ocr' --exclude='/app/ocr/scan' \
      "$HERE/" "$DEST/" || exit 1
  xattr -dr com.apple.quarantine "$DEST" 2>/dev/null
  exec "$DEST/启动报销助手.command"
fi

cd "$HERE/app" || exit 1

if ! command -v uv >/dev/null 2>&1; then
  echo "首次运行：安装 uv（Python 包管理器）…"
  curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv 安装失败，请检查网络后重新双击本文件。"
    exit 1
  fi
fi

if [ ! -x .venv/bin/python ]; then
  echo "首次运行：创建虚拟环境…"
  uv venv --python ">=3.10,<3.14" .venv || exit 1
fi
if ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "安装依赖…"
  uv pip install -q -r requirements.txt && cp requirements.txt .venv/requirements.installed || exit 1
fi

if [ ! -x ocr/ocr ] || [ ! -x ocr/scan ] || [ ocr/ocr -ot ocr/ocr.swift ] || [ ocr/scan -ot ocr/scan.swift ]; then
  if ! xcode-select -p >/dev/null 2>&1; then
    echo "需要先安装 Xcode Command Line Tools（用来编译文字识别工具）。"
    echo "马上会弹出安装窗口，装好之后重新双击本文件。"
    xcode-select --install 2>/dev/null
    exit 1
  fi
  echo "首次运行：编译识别工具…"
  swiftc -O ocr/ocr.swift -o ocr/ocr && swiftc -O ocr/scan.swift -o ocr/scan || exit 1
fi

exec .venv/bin/python server.py
