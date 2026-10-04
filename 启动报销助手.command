#!/bin/bash
# 双击启动报销助手（首次运行会自动安装依赖并编译识别工具）
cd "$(dirname "$0")/app" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "首次运行：安装依赖…"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
if [ ! -x ocr/ocr ] || [ ! -x ocr/scan ]; then
  echo "首次运行：编译识别工具…"
  swiftc -O ocr/ocr.swift -o ocr/ocr && swiftc -O ocr/scan.swift -o ocr/scan || exit 1
fi
exec .venv/bin/python server.py
