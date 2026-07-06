"""
load_xbrl_to_db.py

Loads every data/xbrl/{TICKER}_xbrl_facts.csv into the Postgres
xbrl_facts table (see schema in README / the CREATE TABLE you already ran).

Uses an upsert (INSERT ... ON CONFLICT DO UPDATE) keyed on the same
columns as the table's UNIQUE constraint, so re-running this script on
the same CSVs is safe -- it updates existing rows instead of duplicating
them.

Requires a .env file with:
    DATABASE_URL=postgresql+psycopg2://USER@localhost:5432/filinglens

Usage:
    python load_xbrl_to_db.py                  # load every CSV in data/xbrl/
    python load_xbrl_to_db.py --ticker AAPL     # load just one ticker
    python load_xbrl_to_db.py --dry-run         # preview without writing
"""

import argparse
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import os

PROJECT_ROOT = Path(__file__).resolve().parent
XBRL_DIR = PROJECT_ROOT / "data" / "xbrl"

load_dotenv()  # reads .env into environment variables

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL not set. Copy .env.example to .env and fill in your "
        "local Postgres connection string."
    )

# Columns expected in each {TICKER}_xbrl_facts.csv, and how they map to
# the xbrl_facts table. Matches fetch_xbrl.py's build_facts_table() output.
CSV_TO_DB_COLUMNS = {
    "concept": "concept",
    "tag_used": "tag_used",
    "value": "value",
    "unit": "unit",
    "start": "period_start",
    "end": "period_end",
    "fiscal_year": "fiscal_year",
    "fiscal_period": "fiscal_period",
    "form": "form",
    "filed_date": "filed_date",
    "accession_number": "accession_number",
}

UPSERT_SQL = text("""
    INSERT INTO xbrl_facts (
        cik, ticker, company_name, concept, tag_used, value, unit,
        period_start, period_end, fiscal_year, fiscal_period,
        form, filed_date, accession_number
    )
    VALUES (
        :cik, :ticker, :company_name, :concept, :tag_used, :value, :unit,
        :period_start, :period_end, :fiscal_year, :fiscal_period,
        :form, :filed_date, :accession_number
    )
    ON CONFLICT (cik, concept, period_end, form, accession_number)
    DO UPDATE SET
        value = EXCLUDED.value,
        unit = EXCLUDED.unit,
        tag_used = EXCLUDED.tag_used,
        period_start = EXCLUDED.period_start,
        fiscal_year = EXCLUDED.fiscal_year,
        fiscal_period = EXCLUDED.fiscal_period,
        filed_date = EXCLUDED.filed_date,
        ticker = EXCLUDED.ticker,
        company_name = EXCLUDED.company_name
""")


def infer_cik_and_company(csv_path: Path) -> tuple[str, str]:
    """
    fetch_xbrl.py's CSV filenames are '{ticker}_xbrl_facts.csv' and don't
    embed the CIK directly. We recover the CIK/company name from the
    matching raw companyfacts JSON saved alongside it, since that file
    does contain both.
    """
    import json

    ticker = csv_path.stem.replace("_xbrl_facts", "")
    raw_json_path = XBRL_DIR / f"{ticker}_companyfacts_raw.json"

    if not raw_json_path.exists():
        raise FileNotFoundError(
            f"Expected {raw_json_path} alongside {csv_path.name} to recover "
            f"CIK/company name, but it wasn't found."
        )

    with open(raw_json_path) as f:
        raw = json.load(f)

    cik = str(raw.get("cik", "")).zfill(10)
    company_name = raw.get("entityName", ticker)
    return ticker, cik, company_name


def load_one_csv(engine, csv_path: Path, dry_run: bool = False) -> int:
    ticker, cik, company_name = infer_cik_and_company(csv_path)

    df = pd.read_csv(csv_path)
    if df.empty:
        print(f"  {csv_path.name}: no rows, skipping")
        return 0

    records = []
    for _, row in df.iterrows():
        record = {
            "cik": cik,
            "ticker": ticker,
            "company_name": company_name,
        }
        for csv_col, db_col in CSV_TO_DB_COLUMNS.items():
            val = row.get(csv_col)
            # Check per-value, not DataFrame-wide: pd.isna() correctly
            # catches NaN/NaT/None here, whereas a bulk df.where(...) can
            # silently fail to convert NaN -> None for some dtypes, which
            # is what caused fiscal_year=nan to reach Postgres as a literal
            # float instead of SQL NULL (and blow up the INT column).
            if pd.isna(val):
                record[db_col] = None
            elif db_col == "fiscal_year":
                record[db_col] = int(val)  # pandas gives e.g. 2026.0, column is INT
            else:
                record[db_col] = val
        records.append(record)

    print(f"  {csv_path.name}: {len(records)} rows ({ticker}, CIK {cik})")

    if dry_run:
        return len(records)

    with engine.begin() as conn:
        for record in records:
            conn.execute(UPSERT_SQL, record)

    return len(records)


def main():
    parser = argparse.ArgumentParser(description="Load XBRL fact CSVs into Postgres")
    parser.add_argument(
        "--ticker", type=str, default=None,
        help="Load only this ticker's CSV (default: load all found in data/xbrl/)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be loaded without writing to the database"
    )
    args = parser.parse_args()

    if args.ticker:
        csv_files = [XBRL_DIR / f"{args.ticker.upper()}_xbrl_facts.csv"]
        if not csv_files[0].exists():
            print(f"No file found: {csv_files[0]}")
            return
    else:
        csv_files = sorted(XBRL_DIR.glob("*_xbrl_facts.csv"))

    if not csv_files:
        print(f"No XBRL fact CSVs found in {XBRL_DIR}")
        print("Run fetch_xbrl.py or fetch_xbrl_batch.py first.")
        return

    print(f"Connecting to: {DATABASE_URL.split('@')[-1]}")  # don't print credentials
    engine = create_engine(DATABASE_URL)

    if args.dry_run:
        print("DRY RUN -- no changes will be written\n")

    print(f"Found {len(csv_files)} CSV file(s) to load:\n")

    total_rows = 0
    for csv_path in csv_files:
        try:
            total_rows += load_one_csv(engine, csv_path, dry_run=args.dry_run)
        except Exception as e:
            print(f"  FAILED on {csv_path.name}: {e}")

    print(f"\nTotal rows {'previewed' if args.dry_run else 'loaded/updated'}: {total_rows}")

    if not args.dry_run:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM xbrl_facts"))
            count = result.scalar()
            print(f"xbrl_facts table now has {count} total rows.")


if __name__ == "__main__":
    main()