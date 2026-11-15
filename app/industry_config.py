"""Seed lists for web research, by industry: the competitors and acquirers
the Team expects a report in that space to cover. Research starts from these
plus the companies the data room itself names, then verifies and extends
them -- so major players aren't left out in favour of whatever a few
generic searches happen to return.

An industry applies when enough of its keywords appear in the company's
own materials (pitch deck and document summaries). Add industries here.
"""
import re

INDUSTRIES = {
    "poc_diagnostics_tdm": {
        "label": "point-of-care diagnostics / therapeutic drug monitoring",
        "keywords": ["point-of-care", "point of care", "diagnostic", "assay", "immunoassay", "methadone",
                     "therapeutic drug monitoring", "tdm", "toxicology", "eddp", "opioid", "cassette",
                     "reader", "clia", "510(k)", "whole blood", "lateral flow"],
        "min_hits": 3,
        "competitors": ["ARK Diagnostics (ARCHIMED)", "ProciseDx", "SpinChip (bioMerieux)", "Lin-Zhi International (LZI)",
                        "Aegis Sciences", "Millennium Health", "ARUP Laboratories", "Thermo Fisher Scientific (CEDIA/DRI)",
                        "Roche Diagnostics", "Abbott (Alere)", "QuidelOrtho", "Siemens Healthineers"],
        "acquirers": ["Thermo Fisher Scientific", "Roche", "Abbott", "QuidelOrtho", "bioMerieux", "ARCHIMED",
                      "Siemens Healthineers", "Danaher (Cepheid/Beckman Coulter)", "Bio-Rad", "LabCorp", "Quest Diagnostics"],
    },
    "medical_devices": {
        "label": "medical devices",
        "keywords": ["medical device", "fda", "510(k)", "de novo", "pma", "implant", "catheter", "surgical",
                     "iso 13485", "class ii"],
        "min_hits": 3,
        "competitors": [],
        "acquirers": ["Medtronic", "Boston Scientific", "Stryker", "Johnson & Johnson MedTech", "Abbott", "BD",
                      "Edwards Lifesciences", "Zimmer Biomet", "Baxter"],
    },
    "organ_transplant": {
        "label": "organ preservation / transplant",
        "keywords": ["organ", "transplant", "perfusion", "preservation", "donor", "biobank", "tissue bank"],
        "min_hits": 3,
        "competitors": ["TransMedics", "XVIVO Perfusion", "OrganOx", "Paragonix Technologies", "Bridge to Life",
                        "Organ Recovery Systems"],
        "acquirers": ["TransMedics", "XVIVO", "Getinge", "Terumo", "Medtronic", "Baxter"],
    },
}


def detect(text: str) -> list[dict]:
    """Industries whose keywords appear often enough in `text`."""
    low = text.lower()
    found = []
    for key, ind in INDUSTRIES.items():
        hits = sum(1 for k in ind["keywords"] if re.search(re.escape(k), low))
        if hits >= ind["min_hits"]:
            found.append({"key": key, "hits": hits, **ind})
    return sorted(found, key=lambda d: -d["hits"])
