"""Parse reference/Due_Diligence_Report_Prompts.xlsx into structured JSON.

This is a mechanical extraction pass only: read every non-empty row, carry
forward the last-seen Category into blank-Category continuation rows, and
classify each record by its Status column (blank => core section-drafting
prompt, "DD Assessment" / "Life Science Assessment" => QA/scoring prompt).

Dependency inference and editorial flags are layered on top by hand in
docs/prompt_inventory.md -- this script only produces the raw structured
facts (docs/prompt_inventory.json) that that write-up is built from.
"""
import json

import openpyxl

SRC = "reference/Due_Diligence_Report_Prompts.xlsx"
OUT = "docs/prompt_inventory.json"

COLS = [
    "status",
    "category",
    "logistics",
    "prompt",
    "example_outputs",
    "training_documents",
    "gap_fill_prompt",
]


def clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def main():
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb["Due Diligence"]

    records = []
    last_category = None
    for i, row in enumerate(ws.iter_rows(min_row=6, values_only=True), start=6):
        vals = [clean(v) for v in row[:7]]
        if all(v is None for v in vals):
            continue
        rec = dict(zip(COLS, vals))
        # Skip literal repeated header rows.
        if rec["category"] == "Category" and rec["prompt"] == "Prompt":
            last_category = None
            continue
        if rec["category"] is not None:
            last_category = rec["category"]
        rec["row"] = i
        rec["effective_category"] = rec["category"] or last_category
        rec["is_continuation"] = rec["category"] is None
        records.append(rec)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(records)} records to {OUT}")


if __name__ == "__main__":
    main()
