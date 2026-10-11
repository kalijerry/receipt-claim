"""Synthetic UK till receipts with exact ground truth (PIL only).

A smoke test for the benchmark itself, and a first rough reading on a machine without real receipts.
They are NOT a substitute for real photos: one font family, perfectly printed, no creases or fading.
"""
import json
import random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MERCHANTS = ["TESCO EXPRESS", "Sainsbury's Local", "PRET A MANGER", "Costa Coffee", "THE ROYAL OAK", "Wagamama",
             "GREGGS", "Boots", "Leon", "WHSmith", "Little Waitrose", "Pizza Express", "Premier Inn Euston",
             "Euston Station Cafe", "NANDO'S", "The Crown & Anchor"]
ITEMS = ["Latte", "Cappuccino", "Sandwich", "Chicken wrap", "Fish & chips", "Bottled water", "Coke 330ml",
         "Cheese baguette", "Brownie", "Fruit salad", "Americano", "Soup of the day", "Pasta bake", "Orange juice",
         "Crisps", "Pain au chocolat", "Burger meal", "Side salad"]
TOTAL_LABELS = ["TOTAL", "Total", "TOTAL DUE", "AMOUNT DUE", "BALANCE DUE", "TOTAL TO PAY", "Total:"]
CARD_LABELS = ["CARD", "VISA CONTACTLESS", "Mastercard", "CARD PAYMENT", "DEBIT CARD", "Contactless"]
FONTS = ["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/cour.ttf", "C:/Windows/Fonts/lucon.ttf",
         "/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Courier.ttc",
         "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
WIDTH = 34   # characters per printed line


def _fonts():
    return [f for f in FONTS if Path(f).exists()]


def _money(v, sym):
    return f"{sym}{v:.2f}"


def _row(left, right):
    return left + right.rjust(max(1, WIDTH - len(left)))


def _date_text(d, rng):
    fmt = rng.choice(["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y", "%Y-%m-%d", "%d %b %Y", "%d/%m/%y"])
    return d.strftime(fmt)


def make_receipt(rng, today):
    """-> (lines, truth dict)."""
    sym = rng.choices(["£", "", "*"], [80, 15, 5])[0]
    merchant = rng.choice(MERCHANTS)
    day = today - timedelta(days=rng.randint(1, 60))
    items = [(rng.choice(ITEMS), round(rng.uniform(0.8, 14.5), 2)) for _ in range(rng.randint(2, 6))]
    sub = round(sum(p for _, p in items), 2)
    vat = round(sub / 6, 2) if rng.random() < 0.6 else None
    tip = round(sub * 0.1, 2) if rng.random() < 0.12 else None
    total = round(sub + (tip or 0), 2)            # prices are VAT inclusive, VAT is only shown
    lines = [merchant.center(WIDTH).rstrip(), f"{rng.randint(1, 99)} {rng.choice(['High Street', 'Station Road', 'Park Lane'])}".center(WIDTH).rstrip(),
             f"Tel: 020 7{rng.randint(100, 999)} {rng.randint(1000, 9999)}".center(WIDTH).rstrip(), ""]
    stamp = f"{_date_text(day, rng)}  {rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}"
    lines += [stamp, f"Receipt No: {rng.randint(1000, 99999)}", ""]
    for name, price in items:
        lines.append(_row(f"1 x {name}"[:WIDTH - 9], _money(price, sym)))
    lines.append("-" * WIDTH)
    if vat is not None or rng.random() < 0.5:
        lines.append(_row("SUBTOTAL", _money(sub, sym)))
    if tip is not None:
        lines.append(_row("Service charge 10%", _money(tip, sym)))
    if vat is not None:
        lines.append(_row("VAT 20% incl.", _money(vat, sym)))
    lines.append(_row(rng.choice(TOTAL_LABELS), _money(total, sym)))
    lines.append("")
    if rng.random() < 0.2:
        tendered = float(int(total) + rng.choice([1, 5, 10]))
        lines += [_row("CASH", _money(tendered, sym)), _row("CHANGE DUE", _money(round(tendered - total, 2), sym))]
    else:
        lines += [_row(rng.choice(CARD_LABELS), _money(total, sym)),
                  f"XXXX XXXX XXXX {rng.randint(1000, 9999)}", f"Auth Code: {rng.randint(100000, 999999)}"]
    lines += ["", "THANK YOU".center(WIDTH).rstrip(), f"VAT No. GB {rng.randint(100, 999)} {rng.randint(1000, 9999)} {rng.randint(10, 99)}"]
    return lines, {"amount": total, "date": day.isoformat(), "merchant": merchant}


def render(lines, path, font_path, rng):
    """Receipt paper on a grey table, slightly uneven light."""
    font = ImageFont.truetype(font_path, 40)
    pitch = 58
    w, h = 1400, max(1500, len(lines) * pitch + 420)
    shade = rng.randint(100, 135)
    img = Image.new("RGB", (w, h), (shade, shade - 2, shade - 8))
    d = ImageDraw.Draw(img)
    pw = int(font.getlength("M" * (WIDTH + 2))) + 80
    left = (w - pw) // 2
    d.rectangle((left, 150, left + pw, h - 150), fill=(250, 250, 246))
    for i, text in enumerate(lines):
        d.text((left + 40, 220 + i * pitch), text, fill=(28, 28, 28), font=font)
    img.save(path, "JPEG", quality=92)


def generate(out_dir, n=30, seed=1):
    """Writes n receipts + truth.json to out_dir; returns the truth dict."""
    fonts = _fonts()
    if not fonts:
        raise RuntimeError("找不到等宽系统字体，无法生成合成票据")
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = date.today()
    truth = {}
    for i in range(1, n + 1):
        lines, t = make_receipt(rng, today)
        name = f"synth{i:03d}"
        render(lines, out / f"{name}.jpg", rng.choice(fonts), rng)
        truth[name] = t
    (out / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), "utf-8")
    return truth
