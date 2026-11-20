"""Facts computed from the data room's spreadsheets in plain Python -- no LLM.

The cap table, the financial model's scenarios, the funding schedule and
grant awards are tables; a language model summarizing them dropped share
counts and Scenario 5 from the Nov 15 draft. Here they are parsed directly:

- cap table (Carta detailed / summary formats, or any sheet with "Fully
  Diluted Shares" and "Name" columns): classes, outstanding and fully
  diluted totals, the equity plan pool, top holders with percentages;
- securities ledger: the price paid per share for each preferred class;
- tables with a header row -- funding rounds (Round / Pre-Money), grant
  awards (Grant / Amount), scenarios (Scenario / revenue / EBITDA);
- derived figures: note conversion price = valuation cap / fully diluted
  shares, its discount to each preferred price, the next priced round's
  implied price per share and step-up.

The result (jobs.structured_facts) is rendered as "required facts" in the
shared drafting prefix; the sections must state them exactly.
"""
import datetime as dt
import io
import logging
import re

import openpyxl

from app import repo, storage

log = logging.getLogger("structured")


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("$", "")
        pct = s.endswith("%")
        s = s.rstrip("%")
        try:
            return float(s) / (100 if pct else 1)
        except ValueError:
            return None
    return None


def _txt(v) -> str:
    return " ".join(str(v).split()).strip() if v is not None else ""


def _low(v) -> str:
    return _txt(v).lower()


def _money(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:,.1f} million"
    return f"${v:,.0f}"


def _rows(ws) -> list[list]:
    return [list(r) for r in ws.iter_rows(values_only=True)]


def header_tables(rows: list[list]):
    """(header, data rows) for every block that starts with a row of >= 3
    text cells followed by rows carrying numbers; a block ends at an empty
    row."""
    i = 0
    while i < len(rows) - 1:
        r = rows[i]
        texts = [c for c in r if isinstance(c, str) and c.strip()]
        nxt = next((x for x in rows[i + 1:i + 3] if any(c is not None for c in x)), None)
        if len(texts) >= 3 and nxt and sum(1 for c in nxt if _num(c) is not None) >= 2:
            data, j = [], i + 1
            while j < len(rows):
                cur = rows[j]
                if sum(1 for c in cur if isinstance(c, str) and c.strip()) >= 3 and                         not any(_num(c) is not None for c in cur):
                    break  # a new header row starts the next table
                if any(c is not None and _txt(c) for c in cur):
                    data.append(cur)
                elif not (j + 1 < len(rows) and any(c is not None and _txt(c) for c in rows[j + 1])):
                    break  # two blank rows end the table; one is a spacer
                j += 1
            yield [_txt(c) for c in r], data
            i = j
        else:
            i += 1


def _col(header: list[str], *needles: str) -> int | None:
    for k, h in enumerate(header):
        low = h.lower()
        if all(n in low for n in needles):
            return k
    return None


# ------------------------------------------------------------ cap table --

def parse_cap_table(wb) -> dict | None:
    """The cap table sheet listing the most individual holders (Carta's
    "Detailed" rather than its "Intermediate" roll-up)."""
    found = [c for c in (_cap_sheet(ws) for ws in wb.worksheets) if c]
    if not found:
        return None
    best = max(found, key=lambda c: c.pop("_n"))
    return {**best, **_pool(wb)}


def _cap_sheet(ws) -> dict | None:
    rows = _rows(ws)
    for i, r in enumerate(rows):
        header = [_txt(c) for c in r]
        name_c, fd_c = _col(header, "name"), _col(header, "fully diluted shares")
        if name_c is None or fd_c is None:
            continue
        out_c = _col(header, "outstanding shares")
        pfd_c, pout_c = _col(header, "fully diluted ownership"), _col(header, "outstanding ownership")
        holders, fd_total, out_total = [], None, None
        for row in rows[i + 1:]:
            name = _txt(row[name_c]) if name_c < len(row) else ""
            if not name:
                continue
            low = name.lower()
            if low.startswith("fully diluted shares"):
                fd_total = _num(row[fd_c])
            elif low.startswith("total shares outstanding") and out_c is not None:
                out_total = _num(row[out_c])
            elif not low.startswith(("fully diluted", "total", "shares available", "other ")):
                fd = _num(row[fd_c]) if fd_c < len(row) else None
                if fd:
                    holders.append({
                        "name": name, "fully_diluted": fd,
                        "outstanding": _num(row[out_c]) if out_c is not None else None,
                        "pct_fully_diluted": _num(row[pfd_c]) if pfd_c is not None else None,
                        "pct_outstanding": _num(row[pout_c]) if pout_c is not None else None,
                    })
        if not holders:
            continue
        fd_total = fd_total or sum(h["fully_diluted"] for h in holders)
        if out_total is None and out_c is not None:
            out_total = sum(h["outstanding"] or 0 for h in holders)
        holders.sort(key=lambda h: -h["fully_diluted"])
        return {"sheet": ws.title, "fully_diluted": fd_total, "outstanding": out_total,
                "top_holders": holders[:10], "_n": len(holders)}
    return None


def _pool(wb) -> dict:
    """Equity plan pool, options outstanding and shares available, from a
    summary sheet's labelled rows."""
    out = {}
    for ws in wb.worksheets:
        for row in _rows(ws):
            label = _low(row[0]) if row else ""
            nums = [_num(c) for c in row[1:] if _num(c) is not None and _num(c) > 1]
            if not nums:
                continue
            if "equity incentive plan" in label and "plan_pool" not in out:
                out["plan_pool"] = nums[0]
                out["plan_name"] = _txt(row[0])
            elif label.startswith("options and rsus issued") and "options_outstanding" not in out:
                out["options_outstanding"] = nums[-1]
            elif label.startswith("shares available for issuance") and "pool_available" not in out:
                out["pool_available"] = nums[-1]
    return out


def parse_ledger_prices(wb) -> dict[str, float]:
    """Preferred class -> the price per share most of its holders paid."""
    for ws in wb.worksheets:
        rows = _rows(ws)
        for i, r in enumerate(rows):
            header = [_txt(c) for c in r]
            cls_c, price_c = _col(header, "share class"), _col(header, "price paid per share")
            if cls_c is None or price_c is None:
                continue
            seen: dict[str, dict[float, int]] = {}
            for row in rows[i + 1:]:
                cls, price = _txt(row[cls_c]) if cls_c < len(row) else "", _num(row[price_c]) if price_c < len(row) else None
                if "preferred" in cls.lower() and price and price > 0.01:
                    seen.setdefault(cls, {}).setdefault(round(price, 3), 0)
                    seen[cls][round(price, 3)] += 1
            return {cls: max(p, key=p.get) for cls, p in seen.items()}
    return {}


# ------------------------------------------------------ header tables --

def parse_tables(wb) -> dict:
    out = {"rounds": [], "grants": [], "scenarios": []}
    for ws in wb.worksheets:
        for header, data in header_tables(_rows(ws)):
            low = [h.lower() for h in header]
            if any("round" in h for h in low) and any("pre-money" in h or "pre money" in h for h in low):
                rc, ic, dc = _col(header, "round"), _col(header, "instrument"), _col(header, "date")
                ac, pc = _col(header, "amount"), _col(header, "pre")
                postc, disc = _col(header, "post"), _col(header, "discount")
                for row in data:
                    if rc is None or not _txt(row[rc]) or _low(row[rc]).startswith("total"):
                        continue
                    out["rounds"].append({
                        "round": _txt(row[rc]), "instrument": _txt(row[ic]) if ic is not None else "",
                        "date": row[dc].date().isoformat() if dc is not None and isinstance(row[dc], dt.datetime) else _txt(row[dc]) if dc is not None else "",
                        "amount": _num(row[ac]) if ac is not None else None,
                        "pre_money": _num(row[pc]) if pc is not None else None,
                        "post_money": _num(row[postc]) if postc is not None else None,
                        "discount": _num(row[disc]) if disc is not None else None,
                        "source": f"{ws.title}"})
            elif low and "grant" in low[0] or (len(low) > 1 and "grant" in low[1]):
                gc, ph, dc, ac = _col(header, "grant"), _col(header, "phase"), _col(header, "date"), _col(header, "amount")
                for row in data:
                    if gc is None or ac is None or _num(row[ac]) is None:
                        continue
                    out["grants"].append({
                        "grant": _txt(row[gc]), "phase": _txt(row[ph]) if ph is not None else "",
                        "date": row[dc].date().isoformat() if dc is not None and isinstance(row[dc], dt.datetime) else "",
                        "amount": _num(row[ac]), "source": ws.title})
            elif any("scenario" in h for h in low) and any("revenue" in h or "ebitda" in h for h in low):
                sc = _col(header, "scenario")
                for row in data:
                    name = _txt(row[sc]) if sc is not None and sc < len(row) else ""
                    if not name or not any(_num(row[k]) is not None for k in range(len(row)) if k != sc):
                        continue
                    vals = {header[k]: (row[k].date().isoformat() if isinstance(row[k], dt.datetime) else row[k])
                            for k in range(len(header)) if k != sc and header[k] and k < len(row) and row[k] is not None}
                    out["scenarios"].append({"scenario": name, "values": vals, "source": ws.title})
    return out


# ------------------------------------------------------------- compute --

def _as_of(wb) -> str:
    for ws in wb.worksheets[:2]:
        for row in _rows(ws)[:6]:
            for c in row:
                m = re.search(r"as of ([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})", _txt(c), re.I)
                if m:
                    return m.group(1)
    return ""


def compute(job_id) -> dict:
    """Parse every spreadsheet of the job and derive the required facts."""
    caps, prices, tables = [], {}, {"rounds": [], "grants": [], "scenarios": []}
    for f in repo.list_dataroom_files(job_id):
        if not f["original_filename"].lower().endswith((".xlsx", ".xlsm")):
            continue
        d = f.get("digest") or {}
        if d.get("superseded_by"):
            continue
        try:
            wb = openpyxl.load_workbook(io.BytesIO(storage.get_bytes(f["object_key"])), data_only=True)
        except Exception as exc:
            log.warning("could not open %s: %s", f["original_filename"], exc)
            continue
        name = f.get("source_path") or f["original_filename"]
        cap = parse_cap_table(wb)
        if cap:
            caps.append({**cap, "file": name, "as_of": _as_of(wb)})
        for cls, p in parse_ledger_prices(wb).items():
            prices.setdefault(cls, {"price": p, "file": name})
        t = parse_tables(wb)
        for k in tables:
            tables[k] += [{**x, "file": name} for x in t[k]]
    cap = max(caps, key=lambda c: (bool(c.get("plan_pool")), c["fully_diluted"] or 0)) if caps else None
    out = {"cap_table": cap, "preferred_prices": prices, **tables, "facts": []}
    out["facts"] = _facts(out)
    return out


def _facts(s: dict) -> list[str]:
    facts = []
    cap = s.get("cap_table")
    if cap:
        src = f"(per {cap['file']}{', as of ' + cap['as_of'] if cap.get('as_of') else ''})"
        if cap.get("outstanding"):
            facts.append(f"Shares outstanding: {cap['outstanding']:,.0f}; fully diluted shares: "
                         f"{cap['fully_diluted']:,.0f} {src}.")
        else:
            facts.append(f"Fully diluted shares: {cap['fully_diluted']:,.0f} {src}.")
        if cap.get("plan_pool"):
            pool = f"Equity plan pool ({cap.get('plan_name', 'equity incentive plan')}): {cap['plan_pool']:,.0f} shares"
            if cap.get("options_outstanding"):
                pool += f"; options/RSUs outstanding {cap['options_outstanding']:,.0f}"
            if cap.get("pool_available"):
                pool += f"; available for grant {cap['pool_available']:,.0f}"
            facts.append(pool + f" {src}.")
        for h in cap["top_holders"][:5]:
            pcts = []
            if h.get("pct_outstanding"):
                pcts.append(f"{h['pct_outstanding'] * 100:.1f}% of outstanding")
            if h.get("pct_fully_diluted"):
                pcts.append(f"{h['pct_fully_diluted'] * 100:.1f}% of fully diluted")
            facts.append(f"Holder {h['name']}: {h['fully_diluted']:,.0f} shares fully diluted"
                         + (f" ({', '.join(pcts)})" if pcts else "") + f" {src}.")
    for cls, p in s.get("preferred_prices", {}).items():
        facts.append(f"{cls} price paid per share: ${p['price']:.3f} (per {p['file']}).")
    rounds = s.get("rounds") or []
    notes = [r for r in rounds if "convertible" in (r["instrument"] + r["round"]).lower() and r.get("pre_money")]
    priced = [r for r in rounds if "preferred" in r["instrument"].lower() and (r.get("pre_money") or 0) > 0]
    for r in rounds:
        if r.get("amount"):
            facts.append(f"Funding round {r['round']}: {_money(r['amount'])}"
                         + (f", pre-money {_money(r['pre_money'])}" if r.get("pre_money") else "")
                         + (f", post-money {_money(r['post_money'])}" if r.get("post_money") else "")
                         + (f", {r['date']}" if r.get("date") else "") + f" (per {r['file']}, {r['source']}).")
    if cap and notes:
        note = max(notes, key=lambda r: r.get("date") or "")
        conv = note["pre_money"] / cap["fully_diluted"]
        facts.append(f"COMPUTED: {note['round']} conversion price at its {_money(note['pre_money'])} cap = "
                     f"${conv:.3f} per share ({_money(note['pre_money'])} / {cap['fully_diluted']:,.0f} fully "
                     f"diluted shares).")
        for cls, p in s.get("preferred_prices", {}).items():
            if p["price"] > conv:
                facts.append(f"COMPUTED: ${conv:.3f} is {(1 - conv / p['price']) * 100:.0f}% below the {cls} "
                             f"price of ${p['price']:.3f}.")
        nxt = min(priced, key=lambda r: r.get("date") or "9999") if priced else None
        if nxt and note.get("amount"):
            note_shares = note["amount"] / conv
            price = nxt["pre_money"] / (cap["fully_diluted"] + note_shares)
            facts.append(
                f"COMPUTED: {nxt['round']} at {_money(nxt['pre_money'])} pre-money implies about ${price:.2f} per "
                f"share ({_money(nxt['pre_money'])} / ({cap['fully_diluted']:,.0f} fully diluted + "
                f"{note_shares:,.0f} shares from converting the full {_money(note['amount'])} {note['round']} at "
                f"${conv:.3f}); a {price / conv:.1f}x step-up from the conversion price. Warrants, accrued "
                f"interest or a pool increase would lower this price.")
    for g in s.get("grants") or []:
        facts.append(f"Grant {g['grant']}{' ' + g['phase'] if g['phase'] else ''}: {_money(g['amount'])}"
                     + (f", awarded {g['date']}" if g.get("date") else "") + f" (per {g['file']}).")
    for sc in s.get("scenarios") or []:
        vals = "; ".join(f"{k}: {_fmt(v)}" for k, v in sc["values"].items())
        facts.append(f"Scenario '{sc['scenario']}': {vals} (per {sc['file']}, {sc['source']}).")
    return facts


def _fmt(v) -> str:
    n = _num(v)
    if n is None:
        return _txt(v)
    if float(n).is_integer() and 1900 <= n <= 2100:
        return str(int(n))
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:,.1f}M"
    if abs(n) >= 1_000:
        return f"{n:,.0f}"
    if abs(n) < 1 and n != 0:
        return f"{n * 100:.1f}%"
    return f"{n:g}"


def render(structured: dict | None) -> str:
    if not structured or not structured.get("facts"):
        return ""
    return ("### Required facts computed from the source spreadsheets\n"
            "Exact figures parsed and calculated in code from the cap table, ledger and financial model. State "
            "the ones relevant to your part exactly (they override any conflicting figure elsewhere):\n"
            + "\n".join(f"- {f}" for f in structured["facts"]))
