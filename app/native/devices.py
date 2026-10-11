"""Which backend runs the recognition models fastest on THIS computer (Windows).

Candidates, whatever is installed and detected:
    cpu     onnxruntime on the CPU (always there, the reference)
    dml     onnxruntime-directml: any DirectX 12 GPU (NVIDIA, Intel, AMD)
    ov-cpu  OpenVINO on the CPU (roughly twice as fast as onnxruntime's CPU even on AMD)
    ov-gpu  OpenVINO on a GPU it detects (Intel integrated / Arc; the NVIDIA card also shows up there)

Guessing "a GPU is there, so use it" is wrong often enough: measured here, OpenVINO's GPU path was three times slower
than its CPU path, because it recompiles for every new input size. So the first run times every candidate on receipt-like
pictures of several sizes, drops any whose text differs from the CPU's, and keeps the fastest. The result is cached per
hardware / package combination (change a GPU or a driver package and it is measured again).

BX_OCR_DEVICE=cpu|dml|ov-cpu|ov-gpu forces one (falls back to the choice above when it is not available).
"""
import json
import os
import platform
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

DEVICES = ("cpu", "dml", "ov-cpu", "ov-gpu")
CACHE = Path(os.environ.get("BX_DEVICE_CACHE") or
             Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "receipt-claim" / "device.json")
_OV_TARGET = {"ov-cpu": "CPU", "ov-gpu": "GPU"}
_ov_device = "CPU"
_ov_patched = False


def _version(pkg):
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version(pkg)
    except PackageNotFoundError:
        return None


def _openvino_devices():
    """{"CPU": "AMD Ryzen ...", "GPU": "NVIDIA GeForce ... (dGPU)"}; the NPU is left out (it only runs fixed input sizes)."""
    try:
        from openvino import Core
        import rapidocr.inference_engine.openvino.main  # noqa: F401  (rapidocr's own OpenVINO support must load too)
        core = Core()
        return {d: str(core.get_property(d, "FULL_DEVICE_NAME")) for d in core.available_devices if d in ("CPU", "GPU")}
    except Exception:  # noqa: BLE001  (not installed, or a broken driver: simply not a candidate)
        return {}


def candidates():
    out = ["cpu"]
    try:
        import onnxruntime
        if "DmlExecutionProvider" in onnxruntime.get_available_providers():
            out.append("dml")
    except ImportError:
        pass
    ov = _openvino_devices()
    out += [name for name, target in _OV_TARGET.items() if target in ov]
    return out


def _fingerprint():
    return {"onnxruntime": _version("onnxruntime-directml") or _version("onnxruntime"), "openvino": _version("openvino"),
            "ov_devices": _openvino_devices(), "cpu": platform.processor(), "dml": "dml" in candidates()}


def _install_openvino_patch():
    """rapidocr compiles its OpenVINO models for the string "CPU" only. Route that call to the device we picked."""
    global _ov_patched
    if _ov_patched:
        return
    import rapidocr.inference_engine.openvino.main as ovm
    real_core = ovm.Core

    class _DeviceCore:
        def __init__(self):
            self._core = real_core()

        def set_property(self, *args):
            if args and args[0] == "CPU" and _ov_device != "CPU":
                return None                      # rapidocr tunes the CPU plugin; irrelevant for another device
            return self._core.set_property(*args)

        def compile_model(self, model, device_name="CPU", *args, **kwargs):
            return self._core.compile_model(model=model, device_name=_ov_device, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._core, name)

    ovm.Core = _DeviceCore
    _ov_patched = True


def params(device):
    """rapidocr parameters that make its models run on `device`."""
    global _ov_device
    if device in _OV_TARGET:
        from rapidocr import EngineType
        _install_openvino_patch()
        _ov_device = _OV_TARGET[device]
        return {f"{task}.engine_type": EngineType.OPENVINO for task in ("Det", "Cls", "Rec")}
    return {"EngineConfig.onnxruntime.use_dml": device == "dml"}


def _test_images():
    """Receipt-like pictures in several sizes (the sizes matter: some backends pay again for every new size)."""
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    font = None
    for f in (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "consola.ttf",
              Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "cour.ttf"):
        if f.exists():
            font = ImageFont.truetype(str(f), 40)
            break
    if font is None:
        font = ImageFont.load_default(size=40)
    lines = ["  TEST NOODLE BAR", "  1 HIGH STREET", "", "05/09/2026   12:30", "1 x Ramen         £11.50",
             "1 x Green tea      £2.80", "SUBTOTAL          £14.30", "VAT                £2.38",
             "TOTAL             £14.30", "CARD PAYMENT      £14.30", "Contactless VISA ****1234", "", "   THANK YOU"]
    base = Image.new("RGB", (1200, 1100), (250, 250, 247))
    draw = ImageDraw.Draw(base)
    for i, text in enumerate(lines):
        draw.text((60, 40 + i * 76), text, fill=(25, 25, 25), font=font)
    arr = np.ascontiguousarray(np.asarray(base)[:, :, ::-1])
    return [cv2.resize(arr, (w, h), interpolation=cv2.INTER_AREA)
            for w, h in ((768, 1024), (1024, 1400), (1100, 1800), (1600, 2200), (2200, 1500), (900, 1200))]


def _same(ref, got):
    pairs = [SequenceMatcher(None, "|".join(a), "|".join(b)).ratio() for a, b in zip(ref, got)]
    return bool(pairs) and min(pairs) >= 0.97 and sum(pairs) / len(pairs) >= 0.99


def _cached(fp, cands):
    try:
        data = json.loads(CACHE.read_text("utf-8"))
        if data.get("fingerprint") == fp and data.get("device") in cands:
            return data["device"]
    except (OSError, ValueError):
        pass
    return None


def choose(make, run, log=print):
    """make(device) -> engine; run(engine, image) -> [recognized texts]. Returns the device name to use."""
    cands = candidates()
    forced = os.environ.get("BX_OCR_DEVICE", "auto").lower()
    if forced in DEVICES:
        if forced in cands:
            return forced
        log(f"指定的识别设备 {forced} 在这台电脑上不可用，改为自动选择")
    if len(cands) == 1:
        return "cpu"
    fp = _fingerprint()
    cached = _cached(fp, cands)
    if cached:
        return cached
    log("首次运行：正在为这台电脑选择最快的识别方式（只此一次，约十几秒）……")
    images = _test_images()
    ref, scores = None, {}
    for device in cands:                                  # "cpu" first: it is the reference
        try:
            engine = make(device)
            t0 = time.perf_counter()
            first = [run(engine, im) for im in images]    # every size is new here
            t1 = time.perf_counter() - t0
            t0 = time.perf_counter()
            for im in images:
                run(engine, im)
            t2 = time.perf_counter() - t0
        except Exception as e:  # noqa: BLE001
            log(f"  {device}: 不可用（{str(e).splitlines()[0][:80] if str(e) else type(e).__name__}）")
            continue
        if device == "cpu":
            ref = first
        elif not _same(ref, first):
            log(f"  {device}: 识别结果与 CPU 不一致，不使用")
            continue
        scores[device] = (t1 + t2) / 2
        log(f"  {device}: {scores[device] / len(images):.2f} 秒/张")
    best = min(scores, key=scores.get) if scores else "cpu"
    if best != "cpu" and scores[best] > 0.9 * scores.get("cpu", scores[best] * 2):
        best = "cpu"                                      # not clearly faster: keep the plain, reference path
    log(f"选用：{best}")
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"fingerprint": fp, "device": best, "seconds_per_image": {
            k: round(v / len(images), 3) for k, v in scores.items()},
            "at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass
    return best
