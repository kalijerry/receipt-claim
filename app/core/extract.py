"""Pull amount / date / time / merchant out of OCR lines from a UK receipt.

extract() takes what ONE or TWO independent OCR readers saw. Two readers that agree make a result trustworthy; when they
disagree, or when a date does not fit the photo's time or the trip dates, a warning asks a person to look.
"""
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# £ is often misread as E / € / f / 2 by OCR; '*' is a common receipt prefix.
MONEY_RE = re.compile(r"(GBP|£|€|E|f|\*)?\s?(\d{1,5}[.,]\d{2})(?!\d)")
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

# Only tried when none of the above matched anywhere: OCR slips such as 050CT2026 (05 OCT 2026, O read as 0),
# 9 Oct:'26 (odd punctuation) or O5/O9/2O26 (letters for digits).
FUZZY_DATE_PATTERNS = [
    (re.compile(r"(?<![\d/\-.])(3[01]|[12]\d|0?[1-9])\s?([A-Za-z0-9]{3})[A-Za-z]*[^A-Za-z0-9\n]{0,3}(\d{4}|\d{2})(?!\d)"),
     "dMy"),
    (re.compile(r"(?<![\w/\-.])([0-9OoIl]{1,2})[/\-.]([0-9OoIl]{1,2})[/\-.]([0-9OoIl]{4}|[0-9OoIl]{2})(?!\w)"), "dmy"),
]
DIGITS = str.maketrans("OoIl", "0011")           # letters OCR takes for digits
MONTH_LETTERS = str.maketrans("015", "ols")      # digits OCR takes for letters in a month name (0CT, 5EP, JU1)
LOOKBACK_DAYS = 1100                             # a receipt older than ~3 years is a misread year, not a receipt

# Rows mentioning these are strong evidence of the final amount paid. The second group is matched even glued to
# other words (OCR often drops spaces: "VISACONTACTLESS", "BALANCEDUE").
STRONG_KW = re.compile(r"\b(total|amount|paid|payment|sale|card|tender|contactless|debit|credit|charged)\b|"
                       r"(balance\s?due|amount\s?due|total\s?due|to\s?pay|contactless|mastercard|visa|debit|credit)",
                       re.I)
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
    """Merge OCR observations that sit on the same visual line into a single row of text.

    "rbox" (optional, same layout as "box") is the box moved to the tilt-free height of its printed line,
    for OCR engines that split a line in pieces and photos that are not level; "box" itself stays where the text is.
    """
    rows = []
    for ln in lines:
        x, y, w, h = ln.get("rbox") or ln["box"]
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
    if dt.year < 2000 or (dt - today).days > 1 or (today - dt).days > LOOKBACK_DAYS:
        return None
    return dt


def _month(token, fuzzy):
    """Month number of a 3+ letter token. Fuzzy: digits for letters (0CT) and one wrong character (OC1) are tolerated,
    as long as exactly one month fits."""
    t = token.lower()
    if not fuzzy:
        return MONTHS.get(t[:3])
    t = t.translate(MONTH_LETTERS)[:3]
    if t in MONTHS:
        return MONTHS[t]
    near = [n for name, n in MONTHS.items() if len(t) == 3 and sum(a != b for a, b in zip(t, name)) == 1]
    return near[0] if len(near) == 1 else None


def _scan_dates(rows, patterns, today, fuzzy=False):
    found = []
    for row in rows:
        row = re.sub(r"(20\d{2})(?=([01]?\d|2[0-3]):[0-5]\d)", r"\1 ", row)    # 15/08/202616:56 -> date + time
        for rx, kind in patterns:
            for m in rx.finditer(row):
                a, b, c = m.groups()
                try:
                    if fuzzy and kind == "dmy":
                        a, b, c = a.translate(DIGITS), b.translate(DIGITS), c.translate(DIGITS)
                    if kind == "dmy":
                        dt = _valid_date(int(c), int(b), int(a), today)
                    elif kind == "ymd":
                        dt = _valid_date(int(a), int(b), int(c), today)
                    elif kind == "dmy2":
                        dt = _valid_date(2000 + int(c), int(b), int(a), today)
                    else:
                        mon = _month(b, fuzzy)
                        yr = int(c) if len(c) == 4 else 2000 + int(c)
                        dt = _valid_date(yr, mon, int(a), today) if mon else None
                except (TypeError, ValueError):
                    dt = None
                if dt:
                    found.append(dt)
    return found


def find_dates(rows, today=None):
    today = today or date.today()
    return _scan_dates(rows, DATE_PATTERNS, today) or _scan_dates(rows, FUZZY_DATE_PATTERNS, today, fuzzy=True)


def _in_periods(d, periods):
    return any(a <= d <= b for a, b in periods or [])


def date_prior(d, ctx):
    """How well a date fits what we know besides the print: the photo's time and the trip's dates."""
    ctx = ctx or {}
    p = 0.0
    if ctx.get("exif") and abs((d - ctx["exif"]).days) <= 1:
        p += 0.6
    if _in_periods(d, ctx.get("periods")):
        p += 0.6
    return p


def pick_date(rows, today=None, ctx=None):
    """Most frequent valid date on the receipt (receipts usually print it several times).
    Ties between different dates go to the one that fits the photo's time / the trip dates."""
    found = find_dates(rows, today)
    if not found:
        return None, 0
    counts = defaultdict(int)
    for d in found:
        counts[d] += 1
    best = max(counts, key=lambda d: (counts[d] + date_prior(d, ctx), -found.index(d)))
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
    # A value that is printed on labelled rows (total / balance due / card payment) counts in full each time. Unlabelled
    # rows are mostly item prices: the best of them counts, repeats barely do - otherwise a basket with five items at
    # 1.15 would outweigh the 38.10 that is actually paid, and the more completely an engine reads, the worse it gets.
    labelled = defaultdict(float)
    weak = defaultdict(list)
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
            if neg or STRONG_KW.search(ctx):
                labelled[v] += s
            else:
                weak[v].append(s)
            hits[v] += 1
    scores = defaultdict(float)
    for v in hits:
        w = sorted(weak[v], reverse=True)
        scores[v] = labelled[v] + (w[0] + 0.2 * (len(w) - 1) if w else 0)
    return scores, hits


def rank_amounts(rows):
    """-> (best amount, confidence, top 5 candidates, {candidate: score})"""
    scores, hits = money_candidates(rows)
    good = {v: s for v, s in scores.items() if s > 0}
    if not good:
        return None, 0, [], {}
    ranked = sorted(good.items(), key=lambda kv: (-kv[1], -kv[0]))
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    confidence = round(min(1.0, (top - second) / max(top, 1) + 0.3 * min(hits[best], 3) / 3), 2)
    return best, confidence, [v for v, _ in ranked[:5]], good


def pick_amount(rows):
    best, confidence, alts, _ = rank_amounts(rows)
    return best, confidence, alts


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


def _read_one(ocr, today, ctx):
    """One reader's view: fields, warnings as (code, text), and the amount candidates' scores (for merging)."""
    rows = build_rows(ocr.get("lines", []))
    text = "\n".join(rows)
    amount, amt_conf, alts, scores = rank_amounts(rows)
    dt, date_agree = pick_date(rows, today, ctx)
    warnings = []
    date_source = "ocr"
    if dt is None:
        dt = ctx.get("exif")
        if dt:
            date_source = "photo"
            warnings.append(("date_photo", "票面未识别到日期，已使用照片拍摄日期，请核对"))
        else:
            date_source = "none"
            warnings.append(("date_none", "未识别到日期，请手动填写"))
    elif date_agree < 0.6:
        warnings.append(("date_multi", "票面出现多个不同日期，请核对"))
    if amount is None:
        warnings.append(("amt_none", "未识别到金额，请手动填写"))
    elif amt_conf < 0.5:
        warnings.append(("amt_low", "金额识别把握不高，请核对"))
    if re.search(r"declined", text, re.I):
        warnings.append(("declined", "票据含 DECLINED 字样（可能有一次失败交易），请确认只计一次"))
    return {"amount": amount, "amount_conf": amt_conf, "amount_alts": alts, "_scores": scores,
            "date": dt.isoformat() if dt else None, "date_source": date_source,
            "time": find_time(rows), "merchant": pick_merchant(rows), "text": text, "_w": warnings}


def _merge(a, b, ctx):
    """Two independent readers of the same photo -> one result. Agreement is evidence, disagreement a warning."""
    out = dict(a)
    warn = [w for w in a["_w"] if w[0] in ("declined", "date_multi")]
    for w in b["_w"]:
        if w[0] == "declined" and not any(x[0] == "declined" for x in warn):
            warn.append(w)
    # --- amount
    va, vb = a["amount"], b["amount"]
    agree = {"amount": None, "date": None}
    if va is not None and va == vb:
        agree["amount"] = True
        out["amount_conf"] = max(a["amount_conf"], b["amount_conf"])
    elif va is None or vb is None:
        out["amount"] = va if va is not None else vb
        out["amount_conf"] = (a if va is not None else b)["amount_conf"]
        if out["amount"] is None:
            warn.append(("amt_none", "未识别到金额，请手动填写"))
        else:
            agree["amount"] = False
            warn.append(("amt_one", "两个识别引擎里只有一个读到了金额，请核对"))
    else:
        agree["amount"] = False
        total = defaultdict(float)
        for sc in (a["_scores"], b["_scores"]):
            for v, x in sc.items():
                total[v] += x
        out["amount"] = max(total, key=lambda v: (total[v], v == va))      # tie: the leading reader
        out["amount_conf"] = min(a["amount_conf"], b["amount_conf"])
        warn.append(("amt_differ", f"两个识别引擎读到的金额不同（{va:.2f} / {vb:.2f}），请核对"))
    if out["amount"] is not None:
        extra = [v for v in a["amount_alts"] + b["amount_alts"] if v != out["amount"]]
        out["amount_alts"] = list(dict.fromkeys([out["amount"], *extra]))[:5]
    if out["amount"] is not None and out["amount_conf"] < 0.5 and agree["amount"] is not True:
        warn.append(("amt_low", "金额识别把握不高，请核对"))
    # --- date (only dates read from the print count as a reading)
    da = a["date"] if a["date_source"] == "ocr" else None
    db = b["date"] if b["date_source"] == "ocr" else None
    if da and da == db:
        agree["date"] = True
    elif da and db:
        agree["date"] = False
        dda, ddb = date.fromisoformat(da), date.fromisoformat(db)
        pick = db if date_prior(ddb, ctx) > date_prior(dda, ctx) else da
        out["date"], out["date_source"] = pick, "ocr"
        warn.append(("date_differ", f"两个识别引擎读到的日期不同（{da} / {db}），请核对"))
    elif da or db:
        agree["date"] = False
        out["date"], out["date_source"] = da or db, "ocr"
        d = date.fromisoformat(out["date"])
        if date_prior(d, ctx) == 0:      # fits neither the photo's time nor the trip: one reader alone is not enough
            warn.append(("date_one", f"只有一个识别引擎读到票面日期（{out['date']}），请核对"))
    else:
        warn += [w for w in a["_w"] if w[0] in ("date_photo", "date_none")]
    out["time"] = a["time"] or b["time"]
    out["merchant"] = a["merchant"] or b["merchant"]
    out["agree"] = agree
    out["alt"] = {"amount": vb, "date": b["date"] if b["date_source"] == "ocr" else None}
    out["_w"] = warn
    return out


def _check_date(res, ctx):
    """A printed date must also fit the photo's time and the trip dates, or a person looks at it."""
    if res["date_source"] != "ocr" or not res["date"]:
        return
    d = date.fromisoformat(res["date"])
    exif, periods = ctx.get("exif"), ctx.get("periods")
    inside = _in_periods(d, periods) if periods else None
    if exif and abs((d - exif).days) > 1:
        # Not forgiven even when two readers agree: a wrong date both of them read is exactly what this catches.
        # (A receipt photographed days later is a fair reason - the person decides.)
        res["_w"].append(("date_exif", f"票面日期 {d} 与照片拍摄日期 {exif} 相差 {abs((d - exif).days)} 天，请核对"
                                       + ("（也可能是事后才拍照）" if inside else "")))
        return
    if periods and not inside:
        res["_w"].append(("date_period", f"票面日期 {d} 不在报销日期内，请核对"))


def extract(ocr, today=None, periods=None):
    """ocr: {"lines": [...], "exif_date": "...", "alt": {"lines": [...]}?} -> dict of extracted fields + warnings.

    periods: the trip's [{"start", "end"}]; together with the photo's EXIF time they say which printed dates are plausible.
    "alt" is a second, independent reading of the same photo (Windows backend): the result then also carries
    "agree" ({"amount", "date"}) and "alt" (what the second reader saw).
    """
    today = today or date.today()
    ctx = {"exif": parse_exif(ocr.get("exif_date")), "periods": _parse_periods(periods)}
    res = _read_one(ocr, today, ctx)
    alt = ocr.get("alt")
    if alt:
        res = _merge(res, _read_one({"lines": alt.get("lines", [])}, today, ctx), ctx)
    _check_date(res, ctx)
    res["warnings"] = [text for _, text in res.pop("_w")]
    res.pop("_scores", None)
    return res


def _parse_periods(periods):
    out = []
    for p in periods or []:
        try:
            out.append((date.fromisoformat(p["start"]), date.fromisoformat(p["end"])))
        except (KeyError, TypeError, ValueError):
            pass
    return out


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
