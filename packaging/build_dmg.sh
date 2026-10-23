#!/bin/bash
# 打包 macOS 版：生成 packaging/dist/报销助手/（文件夹，可直接运行）和
# packaging/receipt-claim-macos-v<版本>.dmg
#
# 这个包不冻结 Python，也不预编译识别工具：里面是源码 + 启动报销助手.command，
# 第一次双击时在用户自己的电脑上安装 uv、建虚拟环境、装依赖、编译识别工具（见该脚本）。
# 没有签名也没有公证（没有 Apple 开发者账号），第一次打开会被 Gatekeeper 拦截，
# 需要右键「打开」，详见 packaging/使用说明_macos.txt（会一起放进 DMG 里）。
set -e
cd "$(dirname "$0")/.." || exit 1

VERSION=$(python3 -c "import sys; sys.path.insert(0, 'app'); from version import __version__; print(__version__)")
NAME="报销助手"
DIST="packaging/dist"
STAGE="$DIST/$NAME"
DMG="packaging/receipt-claim-macos-v$VERSION.dmg"

echo "清理旧的打包产物…"
rm -rf "$DIST" "$DMG" "$DMG.sha256"
mkdir -p "$STAGE/app"

echo "复制应用文件…"
rsync -a \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' --exclude='.cache' \
  --exclude='ocr/ocr' --exclude='ocr/scan' --exclude='models' \
  app/ "$STAGE/app/"
cp 启动报销助手.command "$STAGE/"
cp LICENSE "$STAGE/"
cp packaging/使用说明_macos.txt "$STAGE/使用说明.txt"
chmod +x "$STAGE/启动报销助手.command"

echo "生成 DMG（未签名、未公证）…"
hdiutil create -volname "$NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG" -fs HFS+

shasum -a 256 "$DMG" | tee "$DMG.sha256"
echo
echo "完成：$DMG"
echo "未签名：接收者第一次打开\"启动报销助手.command\"时如果被 Gatekeeper 拦截，"
echo "需要右键（或按住 Control 点按）选择「打开」，详见里面的 使用说明.txt。"
