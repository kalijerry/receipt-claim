"""Windows backend, pure Python: RapidOCR (PP-OCR models on onnxruntime) reads the text, OpenCV does the scan geometry
and enhancement, Pillow decodes (pillow-heif adds HEIC) and makes previews.

Mirrors what app/ocr/ocr.swift and app/ocr/scan.swift do on macOS, so the rest of the app cannot tell the difference.
Heavy imports happen inside the functions: importing this module stays cheap.

Two independent readers look at every photo (different model generations and alphabets). Their agreement is what
makes an amount or a date trustworthy; where they differ a person is asked (see core.extract.extract).
Which backend runs the models (onnxruntime CPU, DirectML on any DX12 GPU, OpenVINO on the CPU or an Intel GPU) is
measured on first use and the fastest one that gives the same text as the CPU is kept - see native/devices.py.
Environment: BX_OCR_DEVICE=cpu|dml|ov-cpu|ov-gpu, BX_READERS=en5,ch4 (first one leads).
"""
import os
import threading
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
MODEL_DIR = APP / "models"          # downloaded once (SHA-256 checked by rapidocr), kept outside the venv

_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
# System fonts with both Chinese and '£'. First pair that loads wins (see core.scan.register_fonts).
PDF_FONTS = [(str(_FONTS / "msyh.ttc"), str(_FONTS / "msyhbd.ttc")),     # Microsoft YaHei
             (str(_FONTS / "Deng.ttf"), str(_FONTS / "Dengb.ttf")),      # DengXian
             (str(_FONTS / "simhei.ttf"), None)]

# Phone photos are 3000-4000 px; RapidOCR's default of 2000 px shrinks the text on a long till roll too much.
MAX_SIDE = 3200
# Keep doubtful reads too (default 0.5): every receipt is checked by a person, a missing amount is worse.
TEXT_SCORE = 0.3
# name -> (detector generation, recognizer generation, recognizer language). Measured on real receipts: "en5" reads
# printed dates and '£' far better, "ch4" is the better amount reader on some photos; together they catch each other.
READERS = {"en5": ("PPOCRV5", "PPOCRV5", "EN"), "ch4": ("PPOCRV4", "PPOCRV4", "CH")}

_engines = {}
_device = None                    # backend name from native.devices, decided on first use
_engine_lock = threading.Lock()   # one inference at a time; the scan pool calls in from several threads
_heif_ready = False


def reader_names():
    names = [n.strip() for n in os.environ.get("BX_READERS", "en5,ch4").split(",") if n.strip() in READERS]
    return names or ["en5"]


def engine_info():
    from importlib.metadata import PackageNotFoundError, version

    def v(pkg):
        try:
            return version(pkg)
        except PackageNotFoundError:
            return "?"
    ort = "onnxruntime-directml" if v("onnxruntime-directml") != "?" else "onnxruntime"
    return (f"RapidOCR {v('rapidocr')} readers={'+'.join(reader_names())} ({ort} {v(ort)}, openvino {v('openvino')}, "
            f"device={_device or os.environ.get('BX_OCR_DEVICE', 'auto')}), max_side={MAX_SIDE}, text_score={TEXT_SCORE}")


# ---------------- images ----------------

def _load(path):
    """-> (PIL RGB image with the EXIF orientation applied, EXIF DateTimeOriginal or None)."""
    global _heif_ready
    from PIL import Image, ImageOps
    if not _heif_ready:
        _heif_ready = True
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
    with Image.open(path) as im:
        try:
            taken = im.getexif().get_ifd(0x8769).get(0x9003)      # Exif IFD -> DateTimeOriginal
        except Exception:  # noqa: BLE001  (a damaged EXIF block must not stop the OCR)
            taken = None
        return ImageOps.exif_transpose(im).convert("RGB"), taken


def _bgr(im):
    import numpy as np
    return np.ascontiguousarray(np.asarray(im)[:, :, ::-1])


# ---------------- OCR ----------------

def _build(name, device):
    """A reader ("en5", "ch4") or the detection-only engine ("det", boxes without reading, for the scan geometry).

    rapidocr keeps the use_det / use_cls / use_rec flags of the LAST call on the instance, so a detection-only call
    on a reader would silently turn it into a detector for good: that is why "det" is its own instance.
    """
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
    from . import devices
    det, rec, lang = READERS[reader_names()[0] if name == "det" else name]
    return RapidOCR(params={
        "Global.text_score": TEXT_SCORE, "Global.max_side_len": MAX_SIDE, "Global.log_level": "warning",
        "Global.model_root_dir": str(MODEL_DIR),
        "Global.use_cls": name != "det", "Global.use_rec": name != "det",
        "Det.ocr_version": getattr(OCRVersion, det), "Det.model_type": ModelType.MOBILE,
        "Rec.ocr_version": getattr(OCRVersion, rec), "Rec.model_type": ModelType.MOBILE,
        "Rec.lang_type": getattr(LangRec, lang), **devices.params(device)})


def _run(engine, img):
    out = engine(img)
    return list(getattr(out, "txts", None) or [])


def _rapid(name=None):
    """The reader's (or "det") engine, created on first use. Caller holds _engine_lock."""
    global _device
    name = name or reader_names()[0]
    if name not in _engines:
        try:
            import rapidocr  # noqa: F401
        except ImportError as e:
            raise RuntimeError("缺少文字识别组件 rapidocr，请运行 pip install -r requirements.txt") from e
        if _device is None:
            from . import devices
            _device = devices.choose(lambda device: _build(reader_names()[0], device), _run)
        try:
            _engines[name] = _build(name, _device)
        except Exception as e:  # noqa: BLE001
            if _device != "cpu":         # the chosen backend does not start after all: the CPU gives the same answers
                _device = "cpu"
                _engines.clear()
                return _rapid(name)
            raise RuntimeError(f"文字识别模型加载失败：{e}\n首次运行需要联网下载识别模型（约 30 MB，只此一次）。"
                               f"如果已下载过，请删除 {MODEL_DIR} 后重试") from e
    return _engines[name]


def _infer(name, img):
    """Run one reader (or "det"). If the chosen backend breaks while running, drop to the CPU once and retry."""
    global _device
    with _engine_lock:
        try:
            return _rapid(name)(img)
        except Exception:  # noqa: BLE001
            if _device in (None, "cpu"):
                raise
            _device = "cpu"
            _engines.clear()
            return _rapid(name)(img)


def warmup():
    """Loads (and, the first time, downloads) every reader. The launcher calls this once after installing."""
    with _engine_lock:
        for name in reader_names():
            _rapid(name)
    return engine_info()


def _level_rows(frags, w):
    """Group text fragments into printed lines and give each a tilt-free height -> {index: (y of its line, text height)}.

    PP-OCR cuts a line into pieces at wide gaps ("TOTAL" ... "27.70"). On a photo that is not perfectly level the
    pieces of one line sit at different heights, so a plain "same y" test would pair an amount with the wrong label.
    Each fragment knows the slope of its own baseline; pieces belong together when one lies on the other's baseline.
    frags: [{"cx", "cy", "th", "tw", "ang"}] in pixels; "ang" (radians) is None when too short to tell.
    """
    import math
    slopes = sorted(f["ang"] for f in frags if f["ang"] is not None)
    base = slopes[len(slopes) // 2] if slopes else 0.0        # the photo's overall tilt, for short fragments
    rows = []
    for i in sorted(range(len(frags)), key=lambda i: frags[i]["cy"]):
        f = frags[i]
        for row in rows:
            m = frags[min(row, key=lambda j: abs(frags[j]["cx"] - f["cx"]))]
            ang = ((m["ang"] if m["ang"] is not None else base) + (f["ang"] if f["ang"] is not None else base)) / 2
            off = (f["cy"] - m["cy"]) * math.cos(ang) - (f["cx"] - m["cx"]) * math.sin(ang)
            if abs(off) < 0.6 * min(f["th"], m["th"]):
                row.append(i)
                break
        else:
            rows.append([i])
    level = {}
    for row in rows:
        known = sorted(frags[j]["ang"] for j in row if frags[j]["ang"] is not None)
        ang = known[len(known) // 2] if known else base
        # height of each piece's baseline where it crosses the image's centre line, then the line's average
        y = sum(frags[j]["cy"] - (frags[j]["cx"] - w / 2) * math.tan(ang) for j in row) / len(row)
        for j in row:
            level[j] = (y, frags[j]["th"])
    return level


def _read(img, w, h, name):
    """One reader's lines for a BGR image: [{"text", "conf", "box", "rbox"}] in reading order, normalized boxes."""
    import math
    out = _infer(name, img)
    if getattr(out, "boxes", None) is None or getattr(out, "txts", None) is None:      # nothing found
        return []
    lines, frags = [], []
    for pts, text, conf in zip(out.boxes, out.txts, out.scores):
        pts = [(float(x), float(y)) for x, y in pts]            # numpy floats are not JSON serializable
        x0, x1 = max(0.0, min(p[0] for p in pts)), min(float(w), max(p[0] for p in pts))
        y0, y1 = max(0.0, min(p[1] for p in pts)), min(float(h), max(p[1] for p in pts))
        lines.append({"text": text, "conf": float(conf), "box": [x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h]})
        (ax, ay), (bx, by), (cx, cy), (dx, dy) = pts            # top-left, top-right, bottom-right, bottom-left
        th = (math.hypot(dx - ax, dy - ay) + math.hypot(cx - bx, cy - by)) / 2
        tw = (math.hypot(bx - ax, by - ay) + math.hypot(cx - dx, cy - dy)) / 2
        ang = math.atan2((by - ay) + (cy - dy), (bx - ax) + (cx - dx)) if tw >= 2.5 * th else None
        frags.append({"cx": (ax + bx + cx + dx) / 4, "cy": (ay + by + cy + dy) / 4, "th": max(th, 1.0), "tw": tw,
                      "ang": ang})
    # "rbox": the same box, but at the tilt-free height of its printed line; core.extract.build_rows prefers it
    for i, (y, th) in _level_rows(frags, w).items():
        b = lines[i]["box"]
        lines[i]["rbox"] = [b[0], (y - th / 2) / h, b[2], th / h]
    order = sorted(range(len(lines)), key=lambda i: (lines[i]["rbox"][1] + lines[i]["rbox"][3] / 2, lines[i]["box"][0]))
    return [lines[i] for i in order]


def _ocr_one(path, readers=None):
    """{"file", "lines" (leading reader), "exif_date"?, "alt" (second reader: {"reader", "lines"})?}"""
    out = {"file": str(path), "lines": []}
    try:
        im, taken = _load(path)
    except (OSError, ValueError):       # not a readable image: same as macOS, an empty result
        return out
    if taken:
        out["exif_date"] = taken
    w, h = im.size
    img = _bgr(im)
    names = readers or reader_names()
    out["lines"] = _read(img, w, h, names[0])
    if len(names) > 1:
        out["alt"] = {"reader": names[1], "lines": _read(img, w, h, names[1])}
    return out


def ocr(paths, readers=None):
    """readers: names to run (default: all configured, the first one leads); e.g. ["en5"] for a quick single reading
    or ["ch4"] to get just the second opinion as "lines"."""
    return {str(p): _ocr_one(p, readers) for p in paths}


def lead_readers():
    return reader_names()[:1]


def second_readers():
    return reader_names()[1:2]


def _text_rects(img):
    """Pixel rectangles (x, y, w, h) of all text found in a BGR image (detection only, no reading)."""
    out = _infer("det", img)
    rects = []
    for pts in [] if out.boxes is None else out.boxes:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        rects.append((min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
    return rects


# ---------------- scan ----------------

def _order(pts):
    """4 points -> top-left, top-right, bottom-right, bottom-left."""
    import numpy as np
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s, d = pts.sum(axis=1), pts[:, 1] - pts[:, 0]
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], dtype=np.float32)


def _quad_of(contour, frame_area):
    """Convex contour -> ((tl, tr, br, bl), confidence 0..1), or None when it is too small / the whole frame."""
    import cv2
    import numpy as np
    hull = cv2.convexHull(contour)
    area = cv2.contourArea(hull)
    if not 0.08 * frame_area <= area <= 0.98 * frame_area:
        return None
    approx = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
    quad = approx.reshape(-1, 2) if len(approx) == 4 else cv2.boxPoints(cv2.minAreaRect(hull))
    quad_area = cv2.contourArea(np.asarray(quad, dtype=np.float32))
    return (_order(quad), min(1.0, area / quad_area)) if quad_area > 0 else None


def _find_paper(img, boxes=()):
    """The receipt paper as ((tl, tr, br, bl) in pixels, confidence 0..1), or None.

    Stand-in for Vision's document segmentation. First guess: the paper is the biggest bright blob (good on a darker
    table). If that blob is huge - the table is as bright as the paper - look for the paper's edges instead: the
    smallest quadrilateral that still holds all the text. A wrong guess is harmless, because every scan is OCR'd
    again and compared with the original photo before it may be used.
    """
    import cv2
    import numpy as np
    h, w = img.shape[:2]
    k = min(1.0, 1000 / max(h, w))
    small = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else img
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    frame = gray.size
    size = max(3, int(max(gray.shape) * 0.012)) | 1
    _, mask = cv2.threshold(cv2.GaussianBlur(gray, (5, 5), 0), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (size, size)))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    bright = _quad_of(max(contours, key=cv2.contourArea), frame) if contours else None

    centers = [((x + bw / 2) * k, (y + bh / 2) * k) for x, y, bw, bh in boxes]

    def holds_text(quad):
        return not centers or sum(_inside(c, quad) for c in centers) >= 0.9 * len(centers)

    def area(cand):
        return cv2.contourArea(cand[0])

    if bright and area(bright) <= 0.6 * frame and holds_text(bright[0]):
        return bright[0] / k, bright[1]
    # edges of the paper: close away the ink, then outline what is left
    flat = cv2.GaussianBlur(cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(
        cv2.MORPH_RECT, (size * 2 + 1, size * 2 + 1))), (0, 0), size / 2)
    edges = cv2.dilate(cv2.Canny(flat, 4, 12), np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cands = [c for c in (_quad_of(ct, frame) for ct in contours if cv2.contourArea(ct) > 0.05 * frame)
             if c and area(c) < 0.9 * frame and holds_text(c[0])]
    if bright and holds_text(bright[0]):
        cands.append(bright)
    best = min(cands, key=area) if cands else bright
    return (best[0] / k, best[1]) if best else None


def _inside(p, quad):
    """Is point p inside the convex polygon (points in order)?"""
    sign = 0.0
    for i in range(len(quad)):
        a, b = quad[i], quad[(i + 1) % len(quad)]
        c = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        if c != 0:
            if sign == 0:
                sign = c
            elif (c > 0) != (sign > 0):
                return False
    return True


def _enhance(img):
    """BGR -> grayscale uint8: flatten uneven lighting, push the paper to white, darken ink, sharpen."""
    import cv2
    import numpy as np
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255
    h, w = g.shape
    r = max(6, min(w, h) // 60)
    # Background estimate: a max filter removes the text, a blur smooths it. It is smooth by nature,
    # so on big photos it is computed on a shrunken copy (same result, far less work).
    k = 1 if r < 12 else 2 if r < 24 else 4
    small = cv2.resize(g, (max(1, w // k), max(1, h // k)), interpolation=cv2.INTER_AREA) if k > 1 else g
    rk = max(1, r // k)
    small = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_RECT, (2 * rk + 1, 2 * rk + 1)))
    small = cv2.GaussianBlur(small, (0, 0), 2 * r / k)
    bg = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR) if k > 1 else small
    g = g / np.maximum(bg, 1e-3)                      # evenly lit page
    g = np.clip(g * 1.45 - 0.33, 0, 1) ** 1.35        # levels: ~92% and lighter becomes paper white
    g = np.clip(g + 0.6 * (g - cv2.GaussianBlur(g, (0, 0), 1.6)), 0, 1)   # unsharp mask
    return (g * 255 + 0.5).astype(np.uint8)


def _scan(src, out, mode):
    import cv2
    import numpy as np
    from PIL import Image
    im, _ = _load(src)
    img = _bgr(im)
    full_h, full_w = img.shape[:2]
    cropped, how = False, "full"
    boxes = _text_rects(img) if mode in ("crop", "box") else []

    def box_crop(rects):
        """Straight rectangle around the given rectangles (+ margin), if that actually removes something."""
        nonlocal img, cropped, how
        if not rects:
            return
        pad = max(full_w, full_h) * 0.03
        x0 = max(0.0, min(r[0] for r in rects) - pad)
        y0 = max(0.0, min(r[1] for r in rects) - pad)
        x1 = min(float(full_w), max(r[0] + r[2] for r in rects) + pad)
        y1 = min(float(full_h), max(r[1] + r[3] for r in rects) + pad)
        if (x1 - x0) * (y1 - y0) < full_w * full_h * 0.92:
            img = img[int(y0):int(np.ceil(y1)), int(x0):int(np.ceil(x1))]
            cropped, how = True, "box"

    if mode == "box":
        box_crop(boxes)

    if mode == "crop":
        paper = _find_paper(img, boxes)
        if paper:
            quad, confidence = paper
            # grow the quad a little so text touching the paper edge is never cut
            c = quad.mean(axis=0)
            quad = c + (quad - c) * np.array([1.03, 1.02], dtype=np.float32)
            quad = np.clip(quad, 0, [full_w, full_h]).astype(np.float32)
            tl, tr, br, bl = quad
            area = abs(cv2.contourArea(quad))
            big = area > full_w * full_h * 0.08
            # text lines belonging to this paper must lie completely inside it, otherwise the crop would cut them
            related = [b for b in boxes if _inside((b[0] + b[2] / 2, b[1] + b[3] / 2), quad)]

            def cut(b):
                t = (b[0] + b[3] * 0.2, b[1] + b[3] * 0.2, b[0] + b[2] - b[3] * 0.2, b[1] + b[3] - b[3] * 0.2)
                return not all(_inside(p, quad) for p in ((t[0], t[1]), (t[2], t[1]), (t[0], t[3]), (t[2], t[3])))

            if big and any(cut(b) for b in related):
                qx, qy = quad[:, 0], quad[:, 1]
                box_crop([(qx.min(), qy.min(), qx.max() - qx.min(), qy.max() - qy.min())] + related)
            elif big and confidence > 0.5:
                tw = int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))))
                th = int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))))
                if tw >= 50 and th >= 50:
                    m = cv2.getPerspectiveTransform(quad, np.array([[0, 0], [tw, 0], [tw, th], [0, th]], np.float32))
                    img = cv2.warpPerspective(img, m, (tw, th), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
                    cropped, how = True, "perspective"

    # low-resolution sources (e.g. 768x1024 exports): upscale smoothly so print isn't blocky
    upscale = 1.0
    if img.shape[1] < 1100:
        upscale = min(3.0, 1100 / img.shape[1])
        img = cv2.resize(img, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_LANCZOS4)

    gray = _enhance(img)
    Image.fromarray(gray, "L").save(out, "JPEG", quality=90, dpi=(300, 300))
    return {"ok": True, "cropped": cropped, "how": how, "w": gray.shape[1], "h": gray.shape[0], "upscale": upscale}


def scan_image(src, out, mode):
    import cv2
    try:
        return _scan(src, out, mode)
    except (OSError, ValueError, cv2.error) as e:
        return {"ok": False, "error": str(e)}


# ---------------- preview / folder ----------------

def make_preview(src, dst, size):
    from PIL import Image
    im, _ = _load(src)
    im.thumbnail((size, size), Image.LANCZOS)
    im.save(dst, "JPEG", quality=85)


def open_folder(path):
    os.startfile(str(path))
