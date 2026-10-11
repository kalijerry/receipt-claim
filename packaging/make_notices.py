"""Writes THIRD_PARTY_NOTICES.txt: what is bundled in the .exe and under which licence (read from the installed packages).

Run with the build environment's Python (packaging\\build_exe.bat does).
"""
import re
import sys
from importlib.metadata import distributions
from pathlib import Path

BUNDLED = ["rapidocr", "onnxruntime-directml", "openvino", "opencv-python", "opencv-python-headless", "numpy", "pillow",
           "pillow-heif", "openpyxl", "reportlab", "shapely", "pyclipper", "omegaconf", "pyyaml", "tqdm", "six",
           "colorlog", "requests"]
MODELS = """Recognition models (ONNX files in the "models" folder): PP-OCRv4 / PP-OCRv5 text detection, recognition and
angle models from PaddlePaddle PaddleOCR, converted and distributed by the RapidOCR project. Licence: Apache-2.0.
  https://github.com/PaddlePaddle/PaddleOCR   https://github.com/RapidAI/RapidOCR
DirectML.dll (part of onnxruntime-directml) is Microsoft's redistributable DirectML runtime.
"""


def licence(meta):
    text = meta.get("License-Expression") or ""
    if not text:
        classifiers = [c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
        text = ", ".join(classifiers)
    if not text:
        text = (meta.get("License") or "").strip().splitlines()[0][:80] if (meta.get("License") or "").strip() else "see package"
    return re.sub(r"\s+", " ", text)


def main(out):
    wanted = {name.lower().replace("_", "-") for name in BUNDLED}
    rows = []
    for dist in distributions():
        name = dist.metadata["Name"]
        if name and name.lower().replace("_", "-") in wanted:
            rows.append((name, dist.version, licence(dist.metadata), dist.metadata.get("Home-page") or ""))
    rows.sort(key=lambda r: r[0].lower())
    lines = ["报销助手 / receipt-claim: third-party components bundled in this package", "",
             "The application itself is under the MIT licence (see LICENSE).", "", MODELS, "Python packages:", ""]
    lines += [f"  {n} {v}  --  {lic}  {url}".rstrip() for n, v, lic, url in rows]
    lines += ["", f"Python {sys.version.split()[0]} (PSF licence) and its standard library are included by PyInstaller (GPL-2.0 "
              "with the bootloader exception).", ""]
    Path(out).write_text("\n".join(lines), "utf-8")
    print(f"{len(rows)} packages -> {out}")


if __name__ == "__main__":
    main(Path(__file__).with_name("THIRD_PARTY_NOTICES.txt"))
