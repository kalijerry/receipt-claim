"""Pull amount / date / time / merchant out of OCR lines from a UK receipt."""
import re
from collections import defaultdict
from datetime import date, datetime

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# £ is often misread as E / € / 2 by OCR; '*' is a common receipt prefix.
MONEY_RE = re.compile(r"(GBP|£|€|E|\*)?\s?(\d{1,5}[.,]\d{2})(?!\d)")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\b")

DATE_PATTERNS = [
    # 24/09/2026, 24-09-2026, 24.09.2026, 30/9/2026
    (re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b"), "dmy"),
    # 2026-09-24
    (re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b"), "ymd"),
    # 29/09/26
    (re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2})\b(?![/\-.]?\d)"), "dmy2"),
    # 30 Sep'26, 30 Sep 2026, 30-Sep-26
    (re.compile(r"\b(\d{1,2})[\s\-]?([A-Za-z]{3})[a-z]*[\s'’\-,]*(\d{4}|\d{2})\b"), "dMy"),
]

# Rows mentioning these are strong evidence of the final amount paid.
STRONG_KW = re.compile(r"\b(total|amount|paid|payment|sale|card|tender|contactless|debit|credit|charged)\b", re.I)
# Rows mentioning these are NOT the amount paid.
NEG_KW = re.compile(r"(sub\s?total|net\s?total|tax\s?total|vat\b|tax\b|savings|discount|change\s?due|"
                    r"total\s?qty|items|promo|tip\s?suggest|gross|net\b|rate|price)", re.I)
# Lines that are never the merchant name.
MERCHANT_SKIP = re.compile(r"(duplicate|copy|receipt|tax invoice|table|server|check|welcome|tell us|"
                           r"^\W*$|^\d|^[*\-=#]+$|cardholder|customer|merchant|locator|location|"
                           r"www|http|@|tel|phone|kcal|deal|visit|offer|id:|sale|total|amount|approved|recycle)", re.I)
KNOWN_BRANDS = ["McDonald's", "Burger King", "KFC", "Subway", "Greggs", "Pret", "Costa", "Starbucks",
                "Nando's", "Wagamama", "ASDA", "Tesco", "Sainsbury's", "Lidl", "Aldi", "Waitrose",
                "Co-op", "Morrisons", "M&S", "Boots", "Wetherspoon", "Five Guys", "Leon", "Itsu"]


def build_rows(lines):
    """Merge OCR observations that sit on the same visual line into a single row of text."""
    rows = []
    for ln in lines:
        x, y, w, h = ln["box"]
        cy = y + h / 2
        for r in rows:
            if abs(r["cy"] - cy) < min(r["h"], h) * 0.6:
                r["parts"].append((x, ln["text"]))
                break
        else:
            rows.append({"cy": cy, "h": h, "parts": [(x, ln["text"])]})
    rows.sort(key=lambda r: r["cy"])
    return [" ".join(t for _, t in sorted(r["parts"])) for r in rows]


def _valid_date(y, m, d, today):
    try:
        dt = date(y, m, d)
    except ValueError:
        return None
    if dt.year < 2000 or (dt - today).days > 1:
        return None
    return dt


def find_dates(rows, today=None):
    today = today or date.today()
    found = []
    for row in rows:
        for rx, kind in DATE_PATTERNS:
            for m in rx.finditer(row):
                a, b, c = m.groups()
                try:
                    if kind == "dmy":
                        dt = _valid_date(int(c), int(b), int(a), today)
                    elif kind == "ymd":
                        dt = _valid_date(int(a), int(b), int(c), today)
                    elif kind == "dmy2":
                        dt = _valid_date(2000 + int(c), int(b), int(a), today)
                    else:
                        mon = MONTHS.get(b[:3].lower())
                        yr = int(c) if len(c) == 4 else 2000 + int(c)
                        dt = _valid_date(yr, mon, int(a), today) if mon else None
                except (TypeError, ValueError):
                    dt = None
                if dt:
                    found.append(dt)
    return found


def pick_date(rows, today=None):
    """Most frequent valid date on the receipt (receipts usually print it several times)."""
    found = find_dates(rows, today)
    if not found:
        return None, 0
    counts = defaultdict(int)
    for d in found:
        counts[d] += 1
    best = max(counts, key=lambda d: (counts[d], -found.index(d)))
    agree = counts[best] / len(found)
    return best, agree


def find_time(rows):
    for row in rows:
        m = TIME_RE.search(row)
        if m:
            return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def money_candidates(rows):
    """Score every money-looking value; higher score = more likely the amount actually paid."""
    scores = defaultdict(float)
    hits = defaultdict(int)
    for i, row in enumerate(rows):
        vals = list(MONEY_RE.finditer(row))
        if not vals:
            continue
        # Label is often on the row just above the value (e.g. "Total:" / "£33.92").
        ctx = row if STRONG_KW.search(row) else (rows[i - 1] if i and not MONEY_RE.search(rows[i - 1]) else row)
        neg = bool(NEG_KW.search(row)) and not re.search(r"\btotal\b(?!\s?(qty|savings))", row, re.I) \
            or re.search(r"(sub\s?total|net\s?total|tax\s?total|total\s?savings|vat)", row, re.I)
        for m in vals:
            prefix, num = m.groups()
            v = round(float(num.replace(",", ".")), 2)
            if v <= 0:
                continue
            s = 1.0
            if prefix in ("GBP", "£"):
                s += 1.5
            elif prefix in ("€", "E", "*"):
                s += 0.5
            if STRONG_KW.search(ctx):
                s += 2
            if re.search(r"\btotal\b", ctx, re.I):
                s += 1
            if neg:
                s -= 3
            scores[v] += s
            hits[v] += 1
    return scores, hits


def pick_amount(rows):
    scores, hits = money_candidates(rows)
    good = {v: s for v, s in scores.items() if s > 0}
    if not good:
        return None, 0, []
    ranked = sorted(good.items(), key=lambda kv: (-kv[1], -kv[0]))
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    confidence = round(min(1.0, (top - second) / max(top, 1) + 0.3 * min(hits[best], 3) / 3), 2)
    return best, confidence, [v for v, _ in ranked[:5]]


def pick_merchant(rows):
    blob = " ".join(rows).lower().replace("’", "'")
    for b in KNOWN_BRANDS:
        if re.search(r"\b" + re.escape(b.lower()), blob):
            return b
    for row in rows[:12]:
        t = row.strip()
        if len(t) < 3 or t[0].islower() or MERCHANT_SKIP.search(t):
            continue
        # OCR garbage like "L0O1EL" mixes letters and digits inside one word
        if any(len(w) >= 4 and re.search(r"[A-Za-z]", w) and re.search(r"\d", w) for w in t.split()):
            continue
        letters = sum(c.isalpha() for c in t)
        if letters >= 3 and letters / len(t) > 0.5:
            return t[:40]
    return ""


def parse_exif(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S").date()
    except ValueError:
        return None


def extract(ocr, today=None):
    """ocr: {"lines": [...], "exif_date": "..."} -> dict of extracted fields + warnings."""
    rows = build_rows(ocr.get("lines", []))
    text = "\n".join(rows)
    amount, amt_conf, alts = pick_amount(rows)
    dt, date_agree = pick_date(rows, today)
    warnings = []
    date_source = "ocr"
    if dt is None:
        dt = parse_exif(ocr.get("exif_date"))
        if dt:
            date_source = "photo"
            warnings.append("票面未识别到日期，已使用照片拍摄日期，请核对")
        else:
            date_source = "none"
            warnings.append("未识别到日期，请手动填写")
    elif date_agree < 0.6:
        warnings.append("票面出现多个不同日期，请核对")
    if amount is None:
        warnings.append("未识别到金额，请手动填写")
    elif amt_conf < 0.5:
        warnings.append("金额识别把握不高，请核对")
    if re.search(r"declined", text, re.I):
        warnings.append("票据含 DECLINED 字样（可能有一次失败交易），请确认只计一次")
    return {
        "amount": amount,
        "amount_conf": amt_conf,
        "amount_alts": alts,
        "date": dt.isoformat() if dt else None,
        "date_source": date_source,
        "time": find_time(rows),
        "merchant": pick_merchant(rows),
        "text": text,
        "warnings": warnings,
    }


def locate(lines, amount, day):
    """Where on the photo are the given amount and date printed? -> {"amount": [box], "date": [box]}

    Boxes are normalized [x, y, w, h] with a top-left origin (as stored from the OCR tool).
    If the amount isn't printed exactly, a '£' misread as a leading '2' (12.48 -> 212.48) is accepted.
    """
    out = {"amount": [], "date": []}
    if not lines:
        return out
    fuzzy = []
    for ln in lines:
        text = ln.get("text", "")
        if amount is not None:
            for m in MONEY_RE.finditer(text):
                v = round(float(m.group(2).replace(",", ".")), 2)
                if abs(v - amount) < 0.005:
                    out["amount"].append(ln["box"])
                    break
                t = f"{v:.2f}"
                if t.startswith("2") and len(t) > 4 and abs(float(t[1:]) - amount) < 0.005:
                    fuzzy.append(ln["box"])
        if day and any(dt.isoformat() == day for dt in find_dates([text])):
            out["date"].append(ln["box"])
    if not out["amount"]:
        out["amount"] = fuzzy
    return out
