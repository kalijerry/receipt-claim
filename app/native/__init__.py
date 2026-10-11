"""Everything that depends on the operating system, behind one small interface.

The rest of the app (extraction, claims, Excel, PDF layout, server, UI) is platform independent and only talks
to these functions; each backend module implements them:

    ocr(paths)               -> {str(path): {"file", "lines": [{"text", "conf", "box": [x, y, w, h]}], "exif_date"?}}
                                boxes are normalized, top-left origin, reading order top to bottom
    scan_image(src, out, mode) -> {"ok", "cropped", "how", "w", "h", "upscale"} ; mode = crop | box | enhance
    make_preview(src, dst, size) -> writes a JPEG no larger than size px (browsers cannot show HEIC)
    open_folder(path)
    PDF_FONTS                -> [(regular, bold or None)] font files with both Chinese and '£'

macos.py wraps the Vision / Core Image tools in app/ocr/*.swift; windows.py is pure Python (RapidOCR + OpenCV).
Backends are imported on first use so that importing this package (and the unit tests of the pure logic)
never needs the heavy OS-specific dependencies.
"""
import sys

_impl = None


def impl():
    global _impl
    if _impl is None:
        if sys.platform == "darwin":
            from . import macos as m
        elif sys.platform == "win32":
            from . import windows as m
        else:
            raise RuntimeError("报销助手目前只支持 macOS 和 Windows")
        _impl = m
    return _impl


def ocr(paths, readers=None):
    return impl().ocr(paths, readers)


def warmup():
    """Load the recognition engine now (first run: download models, pick the fastest backend). Returns a description."""
    return getattr(impl(), "warmup", lambda: None)()


def lead_readers():
    """Readers for a quick single look (None = the backend's only reader)."""
    return impl().lead_readers()


def second_readers():
    """The second opinion, to be asked only when the first look was not enough ([] = there is none)."""
    return impl().second_readers()


def scan_image(src, out, mode):
    return impl().scan_image(src, out, mode)


def make_preview(src, dst, size):
    return impl().make_preview(src, dst, size)


def open_folder(path):
    return impl().open_folder(path)


def pdf_fonts():
    return impl().PDF_FONTS


def engine_info():
    """Which recognition engine produced the results (recorded by the benchmark)."""
    return impl().engine_info()
