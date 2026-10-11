"""Degraded copies of a receipt photo (PIL only, so it runs on both platforms).

Real phone photos are rarely as clean as the ones used while tuning; these show how much accuracy depends on it.
Each variant keeps the file stem (the benchmark finds the ground truth by the first 8 characters of the name).
"""
import io
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def _register_heif():
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass


def open_image(path):
    """Opens JPG / PNG / HEIC (when pillow-heif is installed) with the EXIF orientation applied; None if unreadable."""
    _register_heif()
    try:
        with Image.open(path) as im:
            return ImageOps.exif_transpose(im).convert("RGB")
    except (OSError, ValueError):
        return None


def taken_date(path):
    """EXIF DateTimeOriginal of the photo ("2026:09:28 12:31:00") or None."""
    _register_heif()
    try:
        with Image.open(path) as im:
            return im.getexif().get_ifd(0x8769).get(0x9003)
    except (OSError, ValueError, AttributeError):
        return None


def _border(im):
    """Average colour of the four corners: what rotated / warped edges are filled with (not black)."""
    w, h = im.size
    px = [im.getpixel((x, y)) for x in (0, w - 1) for y in (0, h - 1)]
    return tuple(sum(p[i] for p in px) // 4 for i in range(3))


def _rotate(deg):
    return lambda im: im.rotate(deg, resample=Image.BICUBIC, expand=True, fillcolor=_border(im))


def _keystone(im):
    """Shot from slightly below: the top edge is farther away, so it looks narrower."""
    w, h = im.size
    k = 0.07 * w
    # source quadrilateral (upper left, lower left, lower right, upper right) that fills the output rectangle
    quad = (-k, 0, 0, h, w, h, w + k, 0)
    return im.transform(im.size, Image.QUAD, quad, Image.BICUBIC, fillcolor=_border(im))


def _blur(im):
    return im.filter(ImageFilter.GaussianBlur(max(im.size) / 1100))


def _jpeg(im):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=30)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _dark(im):
    return ImageEnhance.Contrast(ImageEnhance.Brightness(im).enhance(0.5)).enhance(0.7)


def _half(im):
    return im.resize((max(1, im.width // 2), max(1, im.height // 2)), Image.LANCZOS)


VARIANTS = {"rot+6": _rotate(6), "rot-6": _rotate(-6), "keystone": _keystone, "blur": _blur,
            "jpeg30": _jpeg, "dark": _dark, "half": _half}


def make_all(src, out_root, names=None):
    """Writes the degraded copies of src to out_root/<variant>/<stem>.jpg -> {variant: path}."""
    im = open_image(src)
    if im is None:
        return {}
    exif = None
    if taken_date(src):                      # keep the photo's time: the app falls back to it when no date is read
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9003] = taken_date(src)
    out = {}
    for name in names or VARIANTS:
        dst = Path(out_root) / name / (Path(src).stem + ".jpg")
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            VARIANTS[name](im).save(dst, "JPEG", quality=92, **({"exif": exif} if exif is not None else {}))
        out[name] = dst
    return out
