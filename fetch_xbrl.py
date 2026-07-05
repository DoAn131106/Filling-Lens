"""
fetch_xbrl.py

Fetches structured XBRL financial facts for a company from SEC's
companyfacts API and saves a clean, tidy time series for a curated
set of financial concepts (Revenues, NetIncomeLoss, R&D expense, etc.)

This is the "ground truth numeric database" for FilingLens -- it sidesteps
the messy pandas.read_html() table-cleaning problem entirely for anything
that's a standard reported financial fact.

Usage:
    python fetch_xbrl.py --ticker AAPL
    python fetch_xbrl.py --cik 0000320193
"""

import argparse
import json
import time
from pathlib import Path

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
XBRL_DIR = PROJECT_ROOT / "data" / "xbrl"
XBRL_DIR.mkdir(parents=True, exist_ok=True)

# SEC requires a descriptive User-Agent with contact info. Replace this
# with your own name/email before running -- SEC will rate-limit or block
# generic/missing User-Agents.
HEADERS = {
    "User-Agent": "FilingLens research project youremail@example.com",
    "Accept-Encoding": "gzip, deflate",
}

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# Curated concepts we care about for FilingLens' core questions.
# Each entry lists candidate XBRL tags in priority order, since filers
# are inconsistent about which tag they use (this is the single biggest
# gotcha with SEC XBRL data -- always check multiple tags, never just one).
CONCEPTS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "cost_of_revenue": [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
    ],
    "gross_profit": [
        "GrossProfit",
    ],
    "research_and_development_expense": [
        "ResearchAndDevelopmentExpense",
    ],
    "operating_income": [
        "OperatingIncomeLoss",
    ],
    "net_income": [
        "NetIncomeLoss",
    ],
    "total_assets": [
        "Assets",
    ],
    "total_liabilities": [
        "Liabilities",
    ],
    "cash_and_equivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
    ],
    "eps_diluted": [
        "EarningsPerShareDiluted",
    ],
}

# Simple in-process rate limiting to stay well under SEC's fair-access
# guidance (they suggest <=10 req/sec; we go much slower since this
# script makes very few requests per run anyway).
REQUEST_DELAY_SECONDS = 0.3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(url: str) -> dict:
    """GET a SEC JSON endpoint with proper headers and a small delay."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.json()


def ticker_to_cik(ticker: str) -> str:
    """Resolve a ticker symbol to a zero-padded 10-digit CIK string."""
    data = _get(TICKERS_URL)
    ticker = ticker.upper()
    for entry in data.values():
        if entry["ticker"].upper() == ticker:
            return str(entry["cik_str"]).zfill(10)
    raise ValueError(f"Ticker '{ticker}' not found in SEC company_tickers.json")


def normalize_cik(cik: str) -> str:
    """Accept CIK with or without leading zeros / 'CIK' prefix."""
    cik = cik.upper().replace("CIK", "").strip()
    return cik.zfill(10)


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def fetch_companyfacts(cik: str) -> dict:
    """Fetch the raw companyfacts JSON blob for a given CIK."""
    url = COMPANYFACTS_URL.format(cik=cik)
    print(f"Fetching companyfacts: {url}")
    return _get(url)


def extract_concept_series(facts: dict, tag_candidates: list[str]) -> pd.DataFrame:
    """
    Given the raw companyfacts JSON and a list of candidate XBRL tags
    (in priority order), return a tidy DataFrame of reported values.

    Returns an empty DataFrame if none of the candidate tags are present.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    for tag in tag_candidates:
        if tag not in us_gaap:
            continue

        units = us_gaap[tag].get("units", {})
        # Prefer USD; fall back to whatever unit is present (e.g. USD/shares for EPS)
        unit_key = "USD" if "USD" in units else next(iter(units), None)
        if unit_key is None:
            continue

        rows = units[unit_key]
        df = pd.DataFrame(rows)
        if df.empty:
            continue

        df["tag_used"] = tag
        df["unit"] = unit_key
        return df

    # No candidate tag found for this concept
    return pd.DataFrame()


def build_facts_table(facts: dict) -> pd.DataFrame:
    """Build one combined tidy DataFrame across all curated concepts."""
    all_rows = []

    for concept_name, tag_candidates in CONCEPTS.items():
        df = extract_concept_series(facts, tag_candidates)
        if df.empty:
            print(f"  - {concept_name}: no data found (tried {tag_candidates})")
            continue

        df["concept"] = concept_name
        all_rows.append(df)
        print(f"  - {concept_name}: {len(df)} rows (tag: {df['tag_used'].iloc[0]})")

    if not all_rows:
        return pd.DataFrame()

    combined = pd.concat(all_rows, ignore_index=True)

    # Keep only the columns that matter for downstream use
    keep_cols = [
        "concept", "tag_used", "val", "unit",
        "start", "end", "fy", "fp", "form", "filed",
        "accn", "frame",
    ]
    for col in keep_cols:
        if col not in combined.columns:
            combined[col] = None
    combined = combined[keep_cols]

    combined = combined.rename(columns={
        "val": "value",
        "accn": "accession_number",
        "fy": "fiscal_year",
        "fp": "fiscal_period",
        "filed": "filed_date",
    })

    # Sort for readability: concept, then chronologically
    combined = combined.sort_values(["concept", "end", "filed_date"]).reset_index(drop=True)
    return combined


def add_derived_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute derived metrics (e.g. gross margin) that aren't directly
    tagged in XBRL, using the raw concepts we already extracted.

    Joins revenue + cost_of_revenue on (end, form, accession_number)
    to compute gross_margin_pct per reporting period per filing.
    """
    derived_rows = []

    rev = df[df["concept"] == "revenue"][["end", "form", "accession_number", "value"]].rename(
        columns={"value": "revenue"}
    )
    gp = df[df["concept"] == "gross_profit"][["end", "form", "accession_number", "value"]].rename(
        columns={"value": "gross_profit"}
    )

    if not rev.empty and not gp.empty:
        merged = pd.merge(rev, gp, on=["end", "form", "accession_number"], how="inner")
        merged["gross_margin_pct"] = (merged["gross_profit"] / merged["revenue"]) * 100
        for _, row in merged.iterrows():
            derived_rows.append({
                "concept": "gross_margin_pct",
                "tag_used": "derived:gross_profit/revenue",
                "value": round(row["gross_margin_pct"], 2),
                "unit": "percent",
                "start": None,
                "end": row["end"],
                "fiscal_year": None,
                "fiscal_period": None,
                "form": row["form"],
                "filed_date": None,
                "accession_number": row["accession_number"],
                "frame": None,
            })

    if derived_rows:
        derived_df = pd.DataFrame(derived_rows)
        if derived_rows:
            derived_df = pd.DataFrame(derived_rows)
            non_empty = [d for d in [df, derived_df] if not d.empty and not d.isna().all().all()]
            df = pd.concat(non_empty, ignore_index=True)
        df = pd.concat([df, derived_df], ignore_index=True)
        print(f"  - gross_margin_pct: {len(derived_rows)} rows (derived)")

    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(ticker: str | None = None, cik: str | None = None):
    if not ticker and not cik:
        raise ValueError("Provide either --ticker or --cik")

    if ticker and not cik:
        print(f"Resolving ticker '{ticker}' to CIK...")
        cik = ticker_to_cik(ticker)
        print(f"  -> CIK {cik}")
    else:
        cik = normalize_cik(cik)

    facts = fetch_companyfacts(cik)
    company_name = facts.get("entityName", "UNKNOWN")
    print(f"\nCompany: {company_name} (CIK {cik})")
    print("Extracting concepts:")

    df = build_facts_table(facts)
    if df.empty:
        print("No XBRL facts extracted -- check CIK / concept tags.")
        return

    df = add_derived_metrics(df)

    # Save raw companyfacts JSON (useful for debugging / adding more concepts later)
    raw_path = XBRL_DIR / f"{ticker or cik}_companyfacts_raw.json"
    with open(raw_path, "w") as f:
        json.dump(facts, f)
    print(f"\nSaved raw companyfacts JSON -> {raw_path}")

    # Save tidy combined CSV
    out_path = XBRL_DIR / f"{ticker or cik}_xbrl_facts.csv"
    df.to_csv(out_path, index=False)
    print(f"Saved tidy XBRL facts table -> {out_path}")
    print(f"\nTotal rows: {len(df)}")
    print(f"Concepts covered: {sorted(df['concept'].unique().tolist())}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch XBRL financial facts from SEC EDGAR")
    parser.add_argument("--ticker", type=str, help="Ticker symbol, e.g. AAPL")
    parser.add_argument("--cik", type=str, help="CIK number, e.g. 0000320193")
    args = parser.parse_args()

    run(ticker=args.ticker, cik=args.cik)