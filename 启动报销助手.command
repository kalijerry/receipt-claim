#!/bin/bash
# 双击启动报销助手（首次运行会自动安装 uv、创建虚拟环境、安装依赖并编译识别工具）
cd "$(dirname "$0")/app" || exit 1
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "首次运行：安装 uv（Python 包管理器）…"
  curl -LsSf https://astral.sh/uv/install.sh | sh || exit 1
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -x .venv/bin/python ]; then
  echo "首次运行：创建虚拟环境并安装依赖…"
  uv venv .venv && uv pip install -q -r requirements.txt || exit 1
fi
if [ ! -x ocr/ocr ] || [ ! -x ocr/scan ]; then
  echo "首次运行：编译识别工具…"
  swiftc -O ocr/ocr.swift -o ocr/ocr && swiftc -O ocr/scan.swift -o ocr/scan || exit 1
fi
exec .venv/bin/python server.py
