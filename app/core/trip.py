"""One business trip = one transaction: periods, receipts, linked groups, export plan.

Trip state lives in output/<trip>/trip.json:
{
  "name": "...", "rate": 50, "currency": "GBP",
  "periods": [{"start": "2026-09-03", "end": "2026-09-06"}, ...],
  "receipts": [{"id", "orig_name", "stored", "sha1", "amount", "date", "time", "merchant",
                "include", "exclude_reason", "group", "note", "ocr": {...}}],
  "groups": {"G1": {"paid": 12.5}}
}
"""
import json
import re
from datetime import date, datetime, timedelta

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif", ".webp"}


def new_trip(name):
    return {"name": name, "rate": 50, "currency": "GBP", "periods": [], "receipts": [], "groups": {}}


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip().strip(".")
    return s or "未命名出差"


def d(s):
    return date.fromisoformat(s) if s else None


# ---------- periods ----------

def period_problems(periods):
    """Validate periods; return list of human readable problems (empty = OK)."""
    out = []
    parsed = []
    for i, p in enumerate(periods, 1):
        try:
            a, b = d(p.get("start")), d(p.get("end"))
        except ValueError:
            out.append(f"第 {i} 段日期格式错误")
            continue
        if not a or not b:
            out.append(f"第 {i} 段缺少开始或结束日期")
            continue
        if b < a:
            out.append(f"第 {i} 段结束日期早于开始日期")
            continue
        parsed.append((a, b, i))
    parsed.sort()
    for (a1, b1, i1), (a2, b2, i2) in zip(parsed, parsed[1:]):
        if a2 <= b1:
            out.append(f"第 {i1} 段与第 {i2} 段日期重叠，重叠天数会被重复计算")
    return out


def total_days(periods):
    """Inclusive day count, overlapping days counted once."""
    days = set()
    for p in periods:
        try:
            a, b = d(p.get("start")), d(p.get("end"))
        except ValueError:
            continue
        if a and b and b >= a:
            days.update(a + timedelta(n) for n in range((b - a).days + 1))
    return len(days)


def in_periods(day, periods):
    if not day:
        return False
    x = d(day)
    for p in periods:
        try:
            a, b = d(p.get("start")), d(p.get("end"))
        except ValueError:
            continue
        if a and b and a <= x <= b:
            return True
    return False


# ---------- duplicates ----------

def mark_duplicates(receipts, new_ids=None):
    """Auto-exclude exact file duplicates and same amount+date+time receipts.

    Only touches receipts in new_ids (so a user's manual decision is never overridden).
    """
    new_ids = set(new_ids) if new_ids is not None else {r["id"] for r in receipts}
    seen_sha, seen_key = {}, {}
    for r in receipts:
        key = (r.get("amount"), r.get("date"), r.get("time")) if r.get("time") and r.get("amount") else None
        dup_of = seen_sha.get(r.get("sha1")) or (seen_key.get(key) if key else None)
        if dup_of and r["id"] in new_ids and not r.get("group"):
            r["include"] = False
            same = "同一文件" if seen_sha.get(r.get("sha1")) else "金额、日期、时间完全相同"
            r["exclude_reason"] = f"疑似重复：与 {dup_of} {same}"
        seen_sha.setdefault(r.get("sha1"), r["orig_name"])
        if key:
            seen_key.setdefault(key, r["orig_name"])


# ---------- manual verification ----------

def fingerprint(trip, r):
    """What the user confirmed. Any later change to these makes the receipt 'unverified' again.

    For a linked receipt the whole group counts: changing, excluding or removing any member (or the
    personal paid amount) changes the claim, so every member must be confirmed again.
    """
    g = r.get("group")
    members = sorted([m["id"], m.get("amount"), bool(m.get("include", True))]
                     for m in trip["receipts"] if g and m.get("group") == g)
    paid = trip.get("groups", {}).get(g, {}).get("paid") if g else None
    return json.dumps([r.get("amount"), r.get("date"), bool(r.get("include", True)), g, paid, members])


def _norm(x):
    """Numbers compare by value (14 == 14.0), so re-saving an amount never undoes a confirmation."""
    if isinstance(x, bool) or x is None or isinstance(x, str):
        return x
    if isinstance(x, (int, float)):
        return round(float(x), 2)
    return [_norm(v) for v in x]


def is_verified(trip, r):
    if not r.get("verified"):
        return False
    try:
        return _norm(json.loads(r["verified"])) == _norm(json.loads(fingerprint(trip, r)))
    except ValueError:
        return False


def set_verified(trip, ids, on=True):
    ids = set(ids)
    for r in trip["receipts"]:
        if r["id"] in ids:
            r["verified"] = fingerprint(trip, r) if on else ""
            r["verified_at"] = datetime.now().isoformat(timespec="seconds") if on else ""


def unverified(trip):
    return [r for r in trip["receipts"] if not is_verified(trip, r)]


# ---------- edits ----------

def sort_periods(periods):
    return sorted(periods, key=lambda p: (p.get("start") or "9999", p.get("end") or ""))


def check_receipt_edit(e):
    """Validate one receipt edit coming from the browser; raises ValueError with a readable message."""
    if e.get("date"):
        try:
            date.fromisoformat(e["date"])
        except (TypeError, ValueError):
            raise ValueError(f"日期格式不对：{e['date']}")
    if e.get("amount") is not None:
        try:
            v = float(e["amount"])
        except (TypeError, ValueError):
            raise ValueError(f"金额不是数字：{e['amount']}")
        if v < 0:
            raise ValueError("金额不能为负数")
        e["amount"] = round(v, 2)


def check_periods(periods):
    for p in periods:
        for k in ("start", "end"):
            if p.get(k):
                try:
                    date.fromisoformat(p[k])
                except (TypeError, ValueError):
                    raise ValueError(f"日期格式不对：{p[k]}")


def normalize_groups(trip):
    """Excluded receipts leave their group; a group with fewer than 2 receipts is dissolved."""
    for r in trip["receipts"]:
        if r.get("group") and not r.get("include", True):
            r["group"] = None
    count = {}
    for r in trip["receipts"]:
        if r.get("group"):
            count[r["group"]] = count.get(r["group"], 0) + 1
    for r in trip["receipts"]:
        if r.get("group") and count[r["group"]] < 2:
            r["group"] = None
    live = {r["group"] for r in trip["receipts"] if r.get("group")}
    groups = {}
    for g, v in (trip.get("groups") or {}).items():
        if g in live:
            paid = v.get("paid")
            if paid is not None and (not isinstance(paid, (int, float)) or paid < 0):
                raise ValueError("个人实付金额必须是不小于 0 的数字")
            groups[g] = {"paid": None if paid is None else round(float(paid), 2)}
    for g in live:
        groups.setdefault(g, {"paid": None})
    trip["groups"] = groups


# ---------- claims (单) ----------

def group_default_paid(members):
    """AA case: one full bill + one personal share -> personal actually paid is the smallest."""
    amts = [m["amount"] for m in members if m.get("amount") is not None]
    return min(amts) if amts else None


def build_claims(trip):
    """Turn receipts into claims. A linked group is ONE claim whose amount = personal actual paid."""
    receipts = trip["receipts"]
    groups = trip.get("groups", {})
    claims = []
    done = set()
    for r in sorted(receipts, key=lambda r: (r.get("date") or "9999", r.get("time") or "99:99", r["orig_name"])):
        if r["id"] in done or not r.get("include", True):
            continue
        gid = r.get("group")
        if gid:
            members = [m for m in receipts if m.get("group") == gid and m.get("include", True)]
            members.sort(key=lambda m: (-(m.get("amount") or 0), m["orig_name"]))  # full bill first
            paid = groups.get(gid, {}).get("paid")
            if paid is None:
                paid = group_default_paid(members)
            dates = sorted(m["date"] for m in members if m.get("date"))
            claims.append({
                "group": gid, "members": members, "amount": paid,
                "date": dates[0] if dates else None,
                "merchant": next((m["merchant"] for m in members if m.get("merchant")), ""),
                "note": "；".join(m["note"] for m in members if m.get("note")),
            })
            done.update(m["id"] for m in members)
        else:
            claims.append({"group": None, "members": [r], "amount": r.get("amount"),
                           "date": r.get("date"), "merchant": r.get("merchant", ""), "note": r.get("note", "")})
            done.add(r["id"])
    # date order; within a day by earliest time (receipts without a time go last), then file name
    claims.sort(key=lambda c: (c["date"] or "9999",
                               min(m.get("time") or "99:99" for m in c["members"]),
                               min(m["orig_name"] for m in c["members"])))
    for i, c in enumerate(claims, 1):
        c["no"] = i
        c["in_period"] = in_periods(c["date"], trip["periods"])
    return claims


def fmt_amount(v):
    return f"{v:.2f}" if v is not None else "未知金额"


def plan_filenames(trip, claims):
    """Name = 序号_金额_日期.ext ; linked group = 序号_金额_日期-1/-2.

    The 序号 equals the claim number (date order), so Finder lists files in date order and they match
    the Excel / PDF numbering. Claims outside the trip dates and excluded receipts go to 未计入/
    (out-of-period ones keep their number). Clashes are checked on the name without extension.
    """
    used = set()   # lower-cased names without extension
    plan = {}      # receipt id -> relative filename

    def free(base, n_members):
        cands = [base] if n_members == 0 else [f"{base}-{k}" for k in range(1, n_members + 1)]
        return not any(c.lower() in used for c in cands)

    def unique(base, n_members=0):
        b, n = base, 2
        while not free(b, n_members):
            b = f"{base} ({n})"
            n += 1
        return b

    width = max(2, len(str(len(claims))))
    for c in claims:
        base = f"{c['no']:0{width}d}_{fmt_amount(c['amount'])}_{c['date'] or '未知日期'}"
        if not c.get("in_period", True):
            base = "未计入/" + base
        if c["group"] and len(c["members"]) > 1:
            b = unique(base, len(c["members"]))
            for k, m in enumerate(c["members"], 1):
                used.add(f"{b}-{k}".lower())
                plan[m["id"]] = f"{b}-{k}{ext_of(m)}"
        else:
            m = c["members"][0]
            b = unique(base)
            used.add(b.lower())
            plan[m["id"]] = b + ext_of(m)
    # excluded receipts are still kept (in a sub folder) so nothing gets lost
    for r in trip["receipts"]:
        if r["id"] not in plan:
            b = unique("未计入/" + f"{fmt_amount(r.get('amount'))}_{r.get('date') or '未知日期'}")
            used.add(b.lower())
            plan[r["id"]] = b + ext_of(r)
    return plan


def ext_of(r):
    m = re.search(r"\.[A-Za-z0-9]+$", r["orig_name"])
    return m.group(0).lower() if m else ".jpg"


def summary(trip, claims=None):
    claims = claims if claims is not None else build_claims(trip)
    days = total_days(trip["periods"])
    cap = round(days * float(trip.get("rate") or 0), 2)
    spent = round(sum(c["amount"] or 0 for c in claims if c["in_period"]), 2)
    out_of_period = round(sum(c["amount"] or 0 for c in claims if not c["in_period"]), 2)
    return {
        "days": days, "rate": trip.get("rate"), "cap": cap, "spent": spent,
        "reimburse": round(min(spent, cap), 2), "over": round(max(0, spent - cap), 2),
        "out_of_period": out_of_period, "claims": len(claims),
        "missing": sum(1 for c in claims if c["amount"] is None or not c["date"]),
    }
