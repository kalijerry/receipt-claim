# PyInstaller spec for the Windows build. Use packaging\build_exe.bat (it prepares the models and calls this).
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
APP = ROOT / "app"
MODELS = APP / "models"

datas = [(str(APP / "static"), "static"),
         (str(MODELS), "models"),                      # recognition models: the package works offline from the first start
         (str(ROOT / "LICENSE"), "."),
         (str(ROOT / "packaging" / "THIRD_PARTY_NOTICES.txt"), ".")]
binaries = []
hiddenimports = ["version", "native.windows", "native.devices", "core.trip", "core.excel", "core.extract", "core.scan"]
for package in ("rapidocr", "pillow_heif", "openvino", "onnxruntime"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis([str(APP / "server.py")],
             pathex=[str(APP)],
             binaries=binaries,
             datas=datas,
             hiddenimports=hiddenimports,
             excludes=["tkinter", "matplotlib", "pytest", "pycel", "pypdfium2", "IPython", "notebook", "scipy",
                       "pandas", "torch", "tensorflow", "paddle", "numba"],
             noarchive=False)

# Keep out what the app never uses: development files, other model formats, the Intel NPU (it only runs fixed input
# sizes, the text models do not), video decoding. The models the app needs are in app/models (see above), so
# rapidocr's own copies of the PP-OCRv6 and angle models are dropped too.
DROP = ("openvino\\include\\", "openvino\\cmake\\", "openvino_intel_npu", "openvino_tensorflow",
        "openvino_pytorch", "openvino_paddle", "openvino_jax", "openvino_gguf", "openvino_hetero", "openvino_auto_batch",
        "openvino\\libs\\cache.json", "opencv_videoio_ffmpeg", "rapidocr\\models\\")


def _drop(dest):
    d = dest.replace("/", "\\").lower()
    return d.endswith(".lib") or any(part in d for part in DROP)       # ".lib" = static import libraries only


a.binaries = [t for t in a.binaries if not _drop(t[0])]
a.datas = [t for t in a.datas if not _drop(t[0])]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [],
          exclude_binaries=True,
          name="报销助手",
          console=True,          # the console is where start-up messages, errors and "Ctrl+C to quit" live
          upx=False)             # UPX-packed binaries are what antivirus tools distrust most
coll = COLLECT(exe, a.binaries, a.datas, name="报销助手", upx=False)
