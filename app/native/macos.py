"""macOS backend: Vision / Core Image through the small Swift tools in app/ocr/, `sips` and `open`."""
import json
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
OCR_BIN = APP / "ocr" / "ocr"
SCAN_BIN = APP / "ocr" / "scan"

# System fonts with both Chinese and '£' (the built-in STSong-Light has no '£' and poor Latin spacing).
PDF_FONTS = [("/System/Library/Fonts/STHeiti Light.ttc", "/System/Library/Fonts/STHeiti Medium.ttc"),
             ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", None)]


def engine_info():
    return "Apple Vision (VNRecognizeTextRequest, accurate, en-GB + zh-Hans)"


def lead_readers():
    return None


def second_readers():
    return []                    # one reader only: Vision


def ocr(paths, readers=None):
    if not paths:
        return {}
    res = subprocess.run([str(OCR_BIN), *map(str, paths)], capture_output=True, text=True, timeout=600)
    out = {}
    for line in res.stdout.splitlines():
        try:
            obj = json.loads(line)
            out[obj["file"]] = obj
        except ValueError:
            pass
    return out


def scan_image(src, out, mode):
    try:
        res = subprocess.run([str(SCAN_BIN), str(src), str(out), mode],
                             capture_output=True, text=True, timeout=180)
        return json.loads(res.stdout or "{}")
    except (ValueError, subprocess.TimeoutExpired):
        return {"ok": False}


def make_preview(src, dst, size):
    subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(size), str(src), "--out", str(dst)],
                   capture_output=True, timeout=60)


def open_folder(path):
    subprocess.run(["open", str(path)])
