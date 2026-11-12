"""Business trips read from a screenshot of the EHR 出差 list.

The list is a table: 开始时间 | 结束时间 | 出差时长 | 单据类型 | 申请时间 | 出差事由 | 审批状态 | 操作.
OCR gives text fragments with boxes; fragments are grouped into table rows by their vertical position, and the
row's fields are found by pattern (dates with minutes = start / end, with seconds = 申请时间) and, where the header
row was read, by the column a fragment sits under.

Reimbursed days: the trip's calendar days, first and last day included (the app's rule for a period). A half day
counts as a whole day, so EHR's 1.5天 (12:00 one day to 18:00 the next) is 2 days; where EHR's duration rounded up
differs from the calendar days the trip is flagged for the user to look at.
"""
import math
import re
from datetime import date

DT = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})日?\s*(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?")
DUR = re.compile(r"(\d+(?:\.\d+)?)\s*天")
HEADERS = {"start": "开始时间", "end": "结束时间", "dur": "出差时长", "type": "单据类型", "applied": "申请时间",
           "reason": "出差事由", "status": "审批状态", "op": "操作"}
NOT_APPROVED = ("驳回", "拒绝", "撤回", "撤销", "作废", "审批中", "待审批", "审核中", "草稿", "未通过")
NOISE = re.compile(r"^(申请|变更|销差|日志|详情|通过|操作)$")


def claim_days(ehr_days):
    """0.5 天算 1 天: any part of a day counts as a whole day."""
    return math.ceil(ehr_days - 1e-9) if ehr_days is not None else None


def _rows(lines):
    """Group fragments into table rows by the vertical centre of their boxes; each row sorted left to right."""
    frags = [ln for ln in lines if ln.get("text", "").strip()]
    if not frags:
        return []
    hs = sorted(ln["box"][3] for ln in frags)
    tol = hs[len(hs) // 2] * 0.6
    rows = []
    for ln in sorted(frags, key=lambda ln: ln["box"][1] + ln["box"][3] / 2):
        cy = ln["box"][1] + ln["box"][3] / 2
        if rows and abs(cy - rows[-1]["cy"]) <= tol:
            r = rows[-1]
            r["items"].append(ln)
            r["cy"] = sum(i["box"][1] + i["box"][3] / 2 for i in r["items"]) / len(r["items"])
        else:
            rows.append({"cy": cy, "items": [ln]})
    return [sorted(r["items"], key=lambda ln: ln["box"][0]) for r in rows]


def _columns(rows):
    """x centre of each column from the header row (the row holding the most header names), or {}."""
    best = {}
    for row in rows:
        cols = {}
        for ln in row:
            t = re.sub(r"\s", "", ln["text"])
            for k, name in HEADERS.items():
                if name in t:
                    cols[k] = ln["box"][0] + ln["box"][2] / 2
        if len(cols) > len(best):
            best = cols
    return best if len(best) >= 3 else {}


def _column_of(ln, cols):
    if not cols:
        return None
    x0, x1 = ln["box"][0], ln["box"][0] + ln["box"][2]
    # the column whose header centre lies inside the fragment, else the nearest header to its left edge
    inside = [k for k, cx in cols.items() if x0 - 0.01 <= cx <= x1 + 0.01]
    if inside:
        return min(inside, key=lambda k: abs(cols[k] - (x0 + x1) / 2))
    return min(cols, key=lambda k: abs(cols[k] - x0) if cols[k] <= x1 else 9)


def _dt(m):
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    try:
        day = date(y, mo, d)
    except ValueError:
        return None
    if h > 23 or mi > 59:
        return None
    return {"date": day.isoformat(), "time": f"{h:02d}:{mi:02d}", "seconds": m.group(6) is not None}


def parse(raw):
    """OCR result of one screenshot -> {"trips": [...], "problems": [...]}.

    trip: {"start", "end" (YYYY-MM-DD), "start_time", "end_time", "ehr_days" (float or None), "days" (calendar days),
           "reason", "status", "approved", "warning"}
    """
    rows = _rows(raw.get("lines", []))
    cols = _columns(rows)
    trips, seen = [], set()
    for row in rows:
        text = " ".join(ln["text"] for ln in row)
        dts = [x for x in (_dt(m) for m in DT.finditer(text)) if x]
        plain = [x for x in dts if not x["seconds"]]       # 申请时间 has seconds, start / end do not
        if len(plain) < 2:
            if len(dts) >= 2 and not cols:                 # no seconds anywhere: first two are start / end
                plain = dts[:2]
            else:
                continue
        start, end = plain[0], plain[1]
        if end["date"] < start["date"]:
            start, end = end, start
        by_col = {}
        for ln in row:
            by_col.setdefault(_column_of(ln, cols), []).append(ln["text"].strip())
        dur_text = " ".join(by_col.get("dur", [])) if cols else text
        dur = DUR.search(dur_text) or DUR.search(text)
        ehr_days = float(dur.group(1)) if dur else None
        if cols and "reason" in by_col:
            reason = " ".join(by_col["reason"])
        else:   # fragments that are neither dates, a duration nor a fixed word
            reason = " ".join(t for ln in row if (t := ln["text"].strip()) and not DT.search(t)
                              and not DUR.fullmatch(t) and not NOISE.match(t))
        status_text = " ".join(by_col.get("status", [])) if cols and "status" in by_col else text
        bad = next((w for w in NOT_APPROVED if w in status_text), None)
        status = bad or ("通过" if "通过" in status_text else "")
        days = (date.fromisoformat(end["date"]) - date.fromisoformat(start["date"])).days + 1
        key = (start["date"], start["time"], end["date"], end["time"])
        if key in seen:
            continue
        seen.add(key)
        warning = ""
        if ehr_days is not None and claim_days(ehr_days) != days:
            warning = f"EHR 记 {ehr_days:g} 天（按 0.5 天算 1 天为 {claim_days(ehr_days)} 天），按日期算 {days} 天，请核对"
        trips.append({"start": start["date"], "end": end["date"], "start_time": start["time"], "end_time": end["time"],
                      "ehr_days": ehr_days, "days": days, "reason": reason.strip(), "status": status,
                      "approved": status == "通过", "warning": warning})
    trips.sort(key=lambda t: (t["start"], t["start_time"]))
    problems = []
    if not trips:
        problems.append("没有在截图里找到出差记录（需要能看清「开始时间」「结束时间」两列）")
    return {"trips": trips, "problems": problems}
