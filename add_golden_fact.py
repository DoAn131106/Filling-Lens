"""
add_golden_fact.py

Small helper to append a manually-verified fact to eval/golden_facts.json,
so building your eval dataset stays a 30-second step each time you
reconcile a new concept, instead of hand-editing JSON.

Usage:
    python add_golden_fact.py \
        --ticker AAPL --cik 0000320193 --concept net_income \
        --tag-used NetIncomeLoss --expected-value 29578000000 --unit USD \
        --period-end 2026-03-28 --fiscal-year 2026 --fiscal-period Q2 \
        --form 10-Q --accession-number 0000320193-26-000013 \
        --source-snippet "Net income $ 29,578 $ 24,780 $ 71,675 $ 61,110" \
        --notes "Confirmed across income statement and cash flow sections."
"""

import argparse
import json
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
GOLDEN_PATH = PROJECT_ROOT / "eval" / "golden_facts.json"


def load_golden() -> dict:
    if not GOLDEN_PATH.exists():
        return {"_readme": "Golden dataset for FilingLens numeric-accuracy eval.", "records": []}
    with open(GOLDEN_PATH) as f:
        return json.load(f)


def save_golden(data: dict):
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(GOLDEN_PATH, "w") as f:
        json.dump(data, f, indent=2)


def make_record_id(ticker: str, fiscal_year: int, fiscal_period: str, concept: str) -> str:
    return f"{ticker}-{fiscal_year}{fiscal_period}-{concept}"


def run(args):
    data = load_golden()

    record_id = make_record_id(args.ticker, args.fiscal_year, args.fiscal_period, args.concept)

    existing_ids = {r["id"] for r in data["records"]}
    if record_id in existing_ids and not args.overwrite:
        print(f"Record '{record_id}' already exists. Use --overwrite to replace it.")
        return

    record = {
        "id": record_id,
        "ticker": args.ticker.upper(),
        "cik": args.cik,
        "concept": args.concept,
        "tag_used": args.tag_used,
        "expected_value": args.expected_value,
        "unit": args.unit,
        "period_end": args.period_end,
        "fiscal_year": args.fiscal_year,
        "fiscal_period": args.fiscal_period,
        "form": args.form,
        "accession_number": args.accession_number,
        "source_snippet": args.source_snippet,
        "verified_by": "manual_reconciliation",
        "verified_date": args.verified_date or str(date.today()),
        "notes": args.notes or "",
    }

    if record_id in existing_ids:
        data["records"] = [r for r in data["records"] if r["id"] != record_id]
        print(f"Overwriting existing record '{record_id}'")

    data["records"].append(record)
    save_golden(data)

    print(f"Added '{record_id}' to {GOLDEN_PATH}")
    print(f"Total golden records: {len(data['records'])}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Append a verified fact to the golden eval dataset")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--cik", required=True)
    parser.add_argument("--concept", required=True)
    parser.add_argument("--tag-used", required=True)
    parser.add_argument("--expected-value", required=True, type=float)
    parser.add_argument("--unit", default="USD")
    parser.add_argument("--period-end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--fiscal-year", required=True, type=int)
    parser.add_argument("--fiscal-period", required=True, help="e.g. Q1, Q2, FY")
    parser.add_argument("--form", required=True, help="e.g. 10-Q, 10-K")
    parser.add_argument("--accession-number", required=True)
    parser.add_argument("--source-snippet", required=True, help="Short confirming excerpt from the filing")
    parser.add_argument("--notes", default="")
    parser.add_argument("--verified-date", default=None, help="Defaults to today")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing record with the same id")
    args = parser.parse_args()

    run(args)