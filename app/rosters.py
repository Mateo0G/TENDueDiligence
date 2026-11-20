"""Rosters merged in Python from every current fact card (P5): people,
agreements, performance metrics and IP events -- so each report gets the
full list it must cover (every consultant, fee, recovery figure,
recordation date), not whatever its retrieval happened to surface.

Also the employer vs competitor check, deterministic: every person's prior
employers against the competitor list (industry seeds + competitors named
in the research memo). Each overlap is a required, disclosed fact for the
Competition and HR reports -- the Nov 15 draft recommended checking whether
team members had worked at competitors when most of the technical team
came from ProciseDx.
"""
import re

from app import evidence, industry_config, repo

_SUFFIX = re.compile(r",?\s+(inc|llc|l\.l\.c|corp|corporation|ltd|limited|gmbh|plc|co|company|lp)\.?$", re.I)


def norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\(.*?\)", "", str(s).lower())).strip()


def _org_tokens(name: str) -> set[str]:
    """Distinctive tokens of an organization name ("ARK Diagnostics
    (ARCHIMED)" -> {"ark", "archimed"}), generic words dropped."""
    generic = {"inc", "llc", "corp", "the", "of", "and", "diagnostics", "health", "healthcare", "sciences",
               "scientific", "laboratories", "labs", "international", "medical", "technologies", "group", "bio"}
    words = re.findall(r"[a-z0-9]{3,}", _SUFFIX.sub("", name.lower()))
    return {w for w in words if w not in generic}


def build(job_id) -> dict:
    files, _ = evidence.unique_files(job_id)
    people: dict[str, dict] = {}
    agreements, metrics, ip_events = {}, {}, {}
    for f in files:
        d = f.get("digest") or {}
        if d.get("superseded_by") or d.get("fallback"):
            continue
        src = f.get("source_path") or f["original_filename"]
        for p in d.get("people") or []:
            key = norm_name(p.get("name", ""))
            if len(key) < 4 or " " not in key:  # need a first and last name
                continue
            e = people.setdefault(key, {"name": p["name"], "roles": [], "prior_employers": [], "equity": [],
                                        "comp": [], "sources": []})
            for k_src, k_dst in (("role", "roles"), ("equity", "equity"), ("comp", "comp")):
                v = (p.get(k_src) or "").strip()
                if v and v not in e[k_dst]:
                    e[k_dst].append(v)
            for emp in p.get("prior_employers") or []:
                if emp and emp not in e["prior_employers"]:
                    e["prior_employers"].append(emp)
            if src not in e["sources"]:
                e["sources"].append(src)
        for a in d.get("agreements") or []:
            key = (norm_name(a.get("counterparty", "")), a.get("type", "").lower(), a.get("date", ""))
            if key[0] and key not in agreements:
                agreements[key] = {**a, "source": src}
        for m in d.get("performance_metrics") or []:
            key = (m.get("metric", "").lower(), m.get("value", ""), m.get("matrix", "").lower())
            if key[0] and key not in metrics:
                metrics[key] = {**m, "source": src}
        for ev in d.get("ip_events") or []:
            key = (re.sub(r"\W", "", ev.get("number", "")), ev.get("event", "").lower(), ev.get("date", ""))
            if key[0] and key not in ip_events:
                ip_events[key] = {**ev, "source": src}
    return {"people": sorted(people.values(), key=lambda p: (-len(p["sources"]), p["name"])),
            "agreements": sorted(agreements.values(), key=lambda a: (a.get("counterparty", ""), a.get("date", ""))),
            "performance_metrics": list(metrics.values()),
            "ip_events": sorted(ip_events.values(), key=lambda e: (e.get("number", ""), e.get("date", "")))}


def competitor_names(context_text: str, research_memo: str) -> list[str]:
    names = []
    for ind in industry_config.detect(context_text):
        names += ind.get("competitors") or []
    # Competitor headings / table rows in the research memo ("### 3. ProciseDx", "| ProciseDx |").
    for m in re.finditer(r"(?m)^#+\s*(?:\d+[.)]\s*)?([A-Z][\w&.\- ]{2,40})", research_memo or ""):
        names.append(m.group(1).strip())
    return list(dict.fromkeys(names))


def employer_overlaps(people: list[dict], competitors: list[str]) -> list[dict]:
    comp_tokens = [(c, _org_tokens(c)) for c in competitors]
    out, seen = [], set()
    for p in people:
        for emp in p["prior_employers"]:
            et = _org_tokens(emp)
            for comp, ct in comp_tokens:
                if ct and et & ct and (p["name"], comp) not in seen:
                    seen.add((p["name"], comp))
                    out.append({"person": p["name"], "employer": emp, "competitor": comp})
                    break
    return out


def render(rosters: dict | None, report: str) -> str:
    """The roster a report must cover, as compact rows."""
    if not rosters:
        return ""
    if report in ("hr",):
        rows = [f"- {p['name']} | {'; '.join(p['roles'])[:200]} | prior: {', '.join(p['prior_employers'])[:160] or '-'}"
                f" | equity: {'; '.join(p['equity'])[:160] or '-'} | comp: {'; '.join(p['comp'])[:120] or '-'}"
                for p in rosters.get("people", [])[:80]]
        title = "People roster (name | role | prior employers | equity | compensation)"
    elif report in ("contracts",):
        rows = [f"- {a.get('counterparty')} | {a.get('type')} | {a.get('date')} | fee: {a.get('fee') or '-'} | "
                f"executed: {a.get('executed')}" for a in rosters.get("agreements", [])[:120]]
        title = "Agreements roster (counterparty | type | date | fee | executed)"
    elif report in ("scitech",):
        rows = [f"- {m.get('metric')}: {m.get('value')} | matrix: {m.get('matrix') or '-'} | n: {m.get('n') or '-'} "
                f"({m.get('source')})" for m in rosters.get("performance_metrics", [])[:80]]
        title = "Performance metrics roster (metric | value | matrix | n)"
    elif report in ("ip",):
        rows = [f"- {e.get('number')} | {e.get('event')} | {e.get('date')} | reel/frame: {e.get('reel_frame') or '-'}"
                for e in rosters.get("ip_events", [])[:80]]
        title = "IP events roster (number | event | date | reel/frame)"
    else:
        rows, title = [], ""
    overlaps = rosters.get("employer_overlaps") or []
    extra = ""
    if overlaps and report in ("hr", "competition", "competition_supp", "executive_summary"):
        extra = ("\nRequired disclosure -- team members who previously worked at a competitor (state each as a "
                 "fact, do not recommend checking it):\n"
                 + "\n".join(f"- {o['person']}: formerly at {o['employer']} (competitor: {o['competitor']})"
                             for o in overlaps))
    if not rows and not extra:
        return ""
    body = (f"### {title} -- cover every row, in a table or in prose\n" + "\n".join(rows)) if rows else ""
    return (body + extra).strip()


def compute_and_store(job_id) -> dict:
    job = repo.get_job(job_id)
    r = build(job_id)
    research = (job.get("research") or {}).get("memo", "")
    r["employer_overlaps"] = employer_overlaps(r["people"], competitor_names(
        evidence.research_context(job_id), research))
    repo.set_job_rosters(job_id, r)
    return {k: len(v) for k, v in r.items()}
