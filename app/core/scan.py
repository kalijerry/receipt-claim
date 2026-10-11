"""Scanned versions of receipts: consistency check against the confirmed data + A4 PDF layout."""
import re
import statistics

import native
from .extract import MONEY_RE, build_rows, find_dates

WORD_RE = re.compile(r"[A-Za-z0-9]{4,}")

# Assumed physical height of one printed receipt text line (Vision box height). Typical thermal print ≈ 3 mm.
LINE_MM = 3.0
# Text never prints smaller than this (only reachable when a very wide photo is capped to the page width).
MIN_LINE_MM = 2.2
# A too-tall receipt may be shrunk to fit one page only while lines stay at least this tall; else it is split.
SHRINK_LINE_MM = 2.6


def facts(ocr):
    """Everything comparable from one OCR result."""
    rows = build_rows(ocr.get("lines", []))
    money = {round(float(m.group(2).replace(",", ".")), 2) for row in rows for m in MONEY_RE.finditer(row)}
    words = {w.upper() for row in rows for w in WORD_RE.findall(row)}
    dates = {d.isoformat() for d in find_dates(rows)}
    heights = [ln["box"][3] for ln in ocr.get("lines", []) if len(ln.get("text", "")) >= 3]
    return {"money": sorted(money), "words": sorted(words), "dates": sorted(dates),
            "line_h": statistics.median(heights) if heights else None}


def merge_facts(a, b):
    """What either of two readings of the same picture showed."""
    return {"money": sorted(set(a["money"]) | set(b["money"])), "words": sorted(set(a["words"]) | set(b["words"])),
            "dates": sorted(set(a["dates"]) | set(b["dates"])), "line_h": a["line_h"] or b["line_h"]}


def facts_all(ocr):
    """facts() over every reader that looked at the picture (the leading one and the optional second one)."""
    f = facts(ocr)
    return merge_facts(f, facts(ocr["alt"])) if ocr.get("alt") else f


def _strip_pound_misread(v):
    """OCR often reads '£' as a leading '2' (e.g. £12.48 -> 212.48)."""
    t = f"{v:.2f}"
    return round(float(t[1:]), 2) if t.startswith("2") and len(t) > 4 else None


def money_match(a, b):
    return abs(a - b) < 0.005 or _strip_pound_misread(a) == b or _strip_pound_misread(b) == a


def reference(orig, full):
    """Content both the original photo and the full-frame scan agree on = reliable receipt content."""
    if not full:
        return orig
    return {"money": [m for m in orig["money"] if any(money_match(m, x) for x in full["money"])],
            "words": sorted(set(orig["words"]) & set(full["words"])), "dates": orig["dates"]}


def coverage(ref, scan):
    """How much of the reference content is still present on the scan (0..1)."""
    money = [m for m in ref["money"] if any(money_match(m, x) for x in scan["money"])]
    words = set(ref["words"]) & set(scan["words"])
    return (round(len(money) / len(ref["money"]), 2) if ref["money"] else 1.0,
            round(len(words) / len(ref["words"]), 2) if ref["words"] else 1.0)


def check(receipt, orig, scan, full=None, cropped=True):
    """Is the scan consistent with what the user confirmed? -> dict of checks + ok flag.

    orig = facts of the original photo, full = facts of the full-frame (uncropped) scan.
    Content completeness only matters for a cropped scan; a full-frame scan cannot lose content.
    """
    amt = receipt.get("amount")
    # the confirmed amount must appear exactly; misread tolerance is only for content coverage
    amount_ok = amt is not None and any(abs(v - amt) < 0.005 for v in scan["money"])
    if receipt.get("date") and receipt["date"] in orig["dates"]:
        date_ok = receipt["date"] in scan["dates"]
    else:
        date_ok = None   # date wasn't printed / was typed by hand: nothing to compare on paper
    money_cov, word_cov = coverage(reference(orig, full), scan)
    content_ok = (money_cov >= 0.75 and word_cov >= 0.6) if cropped else True
    ok = amount_ok and date_ok is not False and content_ok
    return {"ok": ok, "amount_ok": amount_ok, "date_ok": date_ok, "money_cov": money_cov,
            "word_cov": word_cov, "content_ok": content_ok}


def explain(c):
    out = []
    if not c["amount_ok"]:
        out.append("扫描件上找不到已确认的金额")
    if c["date_ok"] is False:
        out.append("扫描件上找不到已确认的日期")
    if not c["content_ok"]:
        out.append(f"扫描件内容不完整（金额覆盖 {c['money_cov']:.0%}，文字覆盖 {c['word_cov']:.0%}）")
    return out


# ---------------- A4 layout ----------------

PAGE_W, PAGE_H = 210.0, 297.0
MARGIN, GAP, CAPTION = 10.0, 6.0, 5.0
USE_W, USE_H = PAGE_W - 2 * MARGIN, PAGE_H - 2 * MARGIN
OVERLAP = 8.0
PRINT_DPI = 350


def natural_size(px_w, px_h, line_h_frac):
    """Physical size (mm) so that text prints at ~LINE_MM; returns (w, h, scale_floor)."""
    if line_h_frac:
        mm_per_px = LINE_MM / (line_h_frac * px_h)
    else:
        mm_per_px = 80.0 / px_w          # unknown: assume 80 mm wide till roll
    w, h = px_w * mm_per_px, px_h * mm_per_px
    # keep within a sane physical range (till rolls are 57–80 mm; photos of 2 receipts can be wider)
    k = 1.0
    if w < 45:
        k = 45 / w
    if w * k > USE_W:
        k = USE_W / w
    return w * k, h * k, (SHRINK_LINE_MM / LINE_MM) / k


def layout(items):
    """items: [{key, px_w, px_h, line_h, caption}] -> pages: [[{key, x, y, w, h, crop:(top,bottom) 0..1, caption}]]

    Receipts are placed in reading order into columns. A receipt too tall for a page is first shrunk
    (text never below SHRINK_LINE_MM), otherwise split into overlapping slices placed one after another.
    """
    pieces = []
    for it in items:
        w, h, floor = natural_size(it["px_w"], it["px_h"], it.get("line_h"))
        max_h = USE_H - CAPTION
        if h > max_h:
            shrink = max_h / h
            if shrink >= floor:
                w, h = w * shrink, h * shrink
        if h <= max_h:
            pieces.append({"key": it["key"], "w": w, "h": h, "crop": (0.0, 1.0), "caption": it["caption"]})
            continue
        # split: each slice holds max_h, consecutive slices overlap by OVERLAP mm
        step = max_h - OVERLAP
        n = 1 + int((h - max_h + step - 1e-6) // step)
        n = max(n, 2)
        for i in range(n):
            top = i * step
            bot = min(h, top + max_h)
            pieces.append({"key": it["key"], "w": w, "h": bot - top, "crop": (top / h, bot / h),
                           "caption": f"{it['caption']}（第 {i + 1}/{n} 段）"})
    # Strict reading order: fill the current column top to bottom, then the next column to the right,
    # then a new page. Earlier pages are never back-filled, so the PDF follows the claim numbers (= dates).
    # Every piece fits on an empty page (height <= max_h, width <= USE_W), so nothing can be dropped.
    bottom, right = MARGIN + USE_H + 1e-6, MARGIN + USE_W + 1e-6
    pages, page = [], []
    x, y, colw = MARGIN, MARGIN, 0.0
    for p in pieces:
        need = p["h"] + CAPTION
        if y + need > bottom and y > MARGIN:          # column full -> next column
            x, y, colw = x + colw + GAP, MARGIN, 0.0
        if x + p["w"] > right and x > MARGIN:          # no room for another column -> next page
            pages.append(page)
            page, x, y, colw = [], MARGIN, MARGIN, 0.0
        page.append({**p, "x": x, "y": y})
        y += need + GAP
        colw = max(colw, p["w"])
    if page:
        pages.append(page)
    return pages


COVER_ROWS_FIRST, COVER_ROWS_NEXT = 30, 46

def register_fonts():
    """-> (regular, bold, has_pound). Uses system fonts with both Chinese and '£' (listed per OS in native/);
    falls back to the built-in CID font, which has no '£' and poor Latin spacing."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    for regular, bold in native.pdf_fonts():
        try:
            if "BX-Regular" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("BX-Regular", regular))
                pdfmetrics.registerFont(TTFont("BX-Bold", bold or regular))
            return "BX-Regular", "BX-Bold", True
        except Exception:  # noqa: BLE001  (font missing or unreadable -> try the next one)
            continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light", "STSong-Light", False


def cover_page_count(n_rows):
    if n_rows <= COVER_ROWS_FIRST:
        return 1
    return 1 + -(-(n_rows - COVER_ROWS_FIRST) // COVER_ROWS_NEXT)


def claim_pages(pages, rows, offset):
    """Page number (1-based, after `offset` cover pages) where each claim's first receipt appears."""
    first = {}
    for n, page in enumerate(pages, 1):
        for p in page:
            first.setdefault(p["key"], n + offset)
    return [min((first[k] for k in r["keys"] if k in first), default=None) for r in rows]


def render_pdf(path, pages, image_of, title="", cover=None):
    """image_of(key) -> path of the scanned JPEG.

    cover = {"head": [(label, value)], "rows": [{"no","date","merchant","n","amount","keys"}], "total": str}
    renders a summary table first; the page column says where each claim's receipts are.
    """
    from PIL import Image
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas

    import io

    FONT, BOLD, has_pound = register_fonts()
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(title)
    n_cover = cover_page_count(len(cover["rows"])) if cover else 0
    total_pages = n_cover + len(pages)

    def tx(text):
        return str(text) if has_pound else str(text).replace("£", "GBP ")

    def fit(text, width_mm, size):
        """Shrink the font a little, then truncate, so text never runs past width_mm."""
        text = tx(text)
        while size > 6 and pdfmetrics.stringWidth(text, FONT, size) > width_mm * mm:
            size -= 0.5
        while text and pdfmetrics.stringWidth(text, FONT, size) > width_mm * mm:
            text = text[:-2] + "…"
        return text, size

    def footer(n):
        c.setFont(FONT, 8)
        c.setFillGray(0.5)
        c.drawRightString((PAGE_W - MARGIN) * mm, 5 * mm, tx(f"{title}　第 {n} / {total_pages} 页"))

    if cover:
        page_of = claim_pages(pages, cover["rows"], n_cover)
        cols = [("单号", 14, "l"), ("日期", 26, "l"), ("商户", 72, "l"), ("张数", 14, "r"),
                ("个人实付", 30, "r"), ("所在页", 18, "r")]
        rows = cover["rows"]
        i, n = 0, 1
        while n <= n_cover:
            y = PAGE_H - MARGIN - 4
            if n == 1:
                c.setFillGray(0)
                c.setFont(BOLD, 18)
                c.drawString(MARGIN * mm, y * mm, title)
                y -= 10
                for label, value in cover["head"]:
                    c.setFont(FONT, 10)
                    c.setFillGray(0.4)
                    c.drawString(MARGIN * mm, y * mm, label)
                    c.setFillGray(0)
                    c.setFont(BOLD if label == "应报销金额" else FONT, 10)
                    c.drawString((MARGIN + 34) * mm, y * mm, tx(value))
                    y -= 6
                y -= 4
            # table header
            c.setFillGray(0.92)
            c.rect(MARGIN * mm, (y - 2) * mm, USE_W * mm, 7 * mm, stroke=0, fill=1)
            c.setFillGray(0.2)
            c.setFont(BOLD, 9)
            x = MARGIN + 2
            for name, w, al in cols:
                (c.drawRightString if al == "r" else c.drawString)(((x + w - 2) if al == "r" else x) * mm, y * mm, name)
                x += w
            y -= 7
            limit = COVER_ROWS_FIRST if n == 1 else COVER_ROWS_NEXT
            for r, pg in list(zip(rows, page_of))[i:i + limit]:
                vals = [f"#{r['no']}", r["date"], r["merchant"], r["n"], r["amount"], pg or "—"]
                x = MARGIN + 2
                c.setFillGray(0)
                for (name, w, al), v in zip(cols, vals):
                    t, size = fit(v, w - 3, 9)
                    c.setFont(FONT, size)
                    (c.drawRightString if al == "r" else c.drawString)(((x + w - 2) if al == "r" else x) * mm, y * mm, t)
                    x += w
                c.setStrokeGray(0.88)
                c.setLineWidth(0.3)
                c.line(MARGIN * mm, (y - 2) * mm, (MARGIN + USE_W) * mm, (y - 2) * mm)
                y -= 6
            i += limit
            if n == n_cover and cover.get("total"):
                c.setFont(BOLD, 10)
                c.setFillGray(0)
                c.drawRightString((MARGIN + USE_W - 20) * mm, (y - 2) * mm, tx(cover["total"]))
            footer(n)
            c.showPage()
            n += 1

    cache = {}
    for n, page in enumerate(pages, n_cover + 1):
        for p in page:
            if p["key"] not in cache:
                cache[p["key"]] = Image.open(image_of(p["key"])).convert("L")
            im = cache[p["key"]]
            top, bot = p["crop"]
            if (top, bot) != (0.0, 1.0):
                im = im.crop((0, round(top * im.height), im.width, round(bot * im.height)))
            # resample to PRINT_DPI at the printed size and embed as JPEG (sharp on paper, small file)
            tw = round(p["w"] / 25.4 * PRINT_DPI)
            if im.width > tw:
                im = im.resize((tw, max(1, round(im.height * tw / im.width))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85, optimize=True)
            buf.seek(0)
            img = ImageReader(buf)   # JPEG bytes are embedded as-is (DCT), not re-encoded
            x, y_top = p["x"] * mm, (PAGE_H - p["y"]) * mm
            cap, size = fit(p["caption"], max(p["w"], 20), 8)
            c.setFont(FONT, size)
            c.setFillGray(0.35)
            c.drawString(x, y_top - 3.5 * mm, cap)
            c.drawImage(img, x, y_top - (CAPTION + p["h"]) * mm, p["w"] * mm, p["h"] * mm)
            c.setStrokeGray(0.8)
            c.setLineWidth(0.3)
            c.rect(x, y_top - (CAPTION + p["h"]) * mm, p["w"] * mm, p["h"] * mm)
        footer(n)
        c.showPage()
    c.save()
