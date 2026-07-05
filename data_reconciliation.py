"""
data_reconciliation.py

Sanity-checks the XBRL fact extraction against the actual filing.

For a given ticker + concept, it:
  1. Pulls the most recent value from data/xbrl/{ticker}_xbrl_facts.csv
  2. Finds the matching parsed filing text (data/parsed/text/) using the
     accession number stored alongside the fact
  3. Searches that text for lines mentioning the concept, so you can
     eyeball whether the XBRL number actually shows up in the filing

This won't auto-confirm correctness (numbers appear in many formats:
"23,636", "$23.6 billion", scaled to millions, etc.) but it gets you
the relevant lines fast instead of manually scrolling a 100-page filing.

Usage:
    python data_reconciliation.py --ticker AAPL --concept net_income
    python data_reconciliation.py --ticker AAPL --concept net_income --form 10-Q
    python data_reconciliation.py --ticker AAPL --list-concepts
"""

import argparse
import re
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
XBRL_DIR = PROJECT_ROOT / "data" / "xbrl"
TEXT_DIR = PROJECT_ROOT / "data" / "parsed" / "text"

# Keywords to search for per concept, since the filing text won't say
# "net_income" -- it'll say "Net income" or "Net income (loss)" etc.
# Add to these lists as you find gaps.
CONCEPT_KEYWORDS = {
    "revenue": ["net sales", "total net sales", "net revenue", "total revenue"],
    "cost_of_revenue": ["cost of sales", "cost of revenue"],
    "gross_profit": ["gross margin", "gross profit"],
    "research_and_development_expense": ["research and development"],
    "operating_income": ["operating income"],
    "net_income": ["net income"],
    "total_assets": ["total assets"],
    "total_liabilities": ["total liabilities"],
    "cash_and_equivalents": ["cash and cash equivalents"],
    "eps_diluted": ["diluted earnings per share", "earnings per share"],
    "gross_margin_pct": ["gross margin"],
}


def load_facts(ticker: str) -> pd.DataFrame:
    path = XBRL_DIR / f"{ticker}_xbrl_facts.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"No facts file at {path}. Run fetch_xbrl.py --ticker {ticker} first."
        )
    df = pd.read_csv(path)
    df["end"] = pd.to_datetime(df["end"], errors="coerce")
    df["filed_date"] = pd.to_datetime(df["filed_date"], errors="coerce")
    return df


def get_most_recent_value(df: pd.DataFrame, concept: str, form: str | None) -> pd.Series:
    subset = df[df["concept"] == concept]
    if form:
        subset = subset[subset["form"] == form]

    if subset.empty:
        raise ValueError(
            f"No rows found for concept='{concept}'"
            + (f", form='{form}'" if form else "")
        )

    subset = subset.sort_values("end", ascending=False)
    return subset.iloc[0]


def find_matching_text_file(accession_number: str) -> Path | None:
    """
    Parsed text filenames are expected to contain the accession number
    (with dashes), matching how fetch_data.py / parse_filing.py name files.
    Falls back to a loose glob if an exact match isn't found.
    """
    if not TEXT_DIR.exists():
        return None

    candidates = list(TEXT_DIR.glob(f"*{accession_number}*"))
    if candidates:
        return candidates[0]

    # Loose fallback: accession number without dashes
    accn_nodash = accession_number.replace("-", "")
    candidates = list(TEXT_DIR.glob(f"*{accn_nodash}*"))
    return candidates[0] if candidates else None


def format_value(value: float, unit: str) -> str:
    if unit == "USD":
        return f"${value:,.0f}"
    if unit == "percent":
        return f"{value:.2f}%"
    return f"{value:,.4f} {unit}"


def search_text_for_keywords(text_path: Path, keywords: list[str], context_chars: int = 200):
    """
    Prints lines/snippets around each keyword occurrence so you can
    eyeball whether the XBRL value shows up in the actual filing text.
    """
    text = text_path.read_text(errors="ignore")
    lower_text = text.lower()

    found_any = False
    for kw in keywords:
        for match in re.finditer(re.escape(kw.lower()), lower_text):
            found_any = True
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + context_chars)
            snippet = text[start:end].replace("\n", " ").strip()
            print(f"    ...{snippet}...")
            print()

    if not found_any:
        print(f"    No occurrences of {keywords} found in {text_path.name}")


def run(ticker: str, concept: str, form: str | None, list_concepts: bool):
    df = load_facts(ticker)

    if list_concepts:
        print(f"Available concepts for {ticker}:")
        for c in sorted(df["concept"].unique()):
            print(f"  - {c}")
        return

    if concept not in df["concept"].unique():
        available = sorted(df["concept"].unique())
        raise ValueError(f"Concept '{concept}' not found. Available: {available}")

    row = get_most_recent_value(df, concept, form)

    print("=" * 70)
    print(f"RECONCILIATION CHECK: {ticker} / {concept}")
    print("=" * 70)
    print(f"XBRL value:         {format_value(row['value'], row['unit'])}")
    print(f"Tag used:           {row['tag_used']}")
    print(f"Period end:         {row['end'].date() if pd.notna(row['end']) else 'N/A'}")
    print(f"Fiscal year/period: {row.get('fiscal_year')} / {row.get('fiscal_period')}")
    print(f"Form:               {row['form']}")
    print(f"Filed:              {row['filed_date'].date() if pd.notna(row['filed_date']) else 'N/A'}")
    print(f"Accession number:   {row['accession_number']}")
    print()

    text_path = find_matching_text_file(str(row["accession_number"]))
    if text_path is None:
        print("Could not find a matching parsed text file in data/parsed/text/.")
        print("Check that parse_filing.py has been run for this filing, and that")
        print("the filename contains the accession number.")
        return

    print(f"Cross-checking against parsed filing text: {text_path.name}")
    print("-" * 70)

    keywords = CONCEPT_KEYWORDS.get(concept, [concept.replace("_", " ")])
    search_text_for_keywords(text_path, keywords)

    print("-" * 70)
    print("Manual step: confirm the XBRL value above appears in one of the")
    print("snippets (numbers in filings are often in millions and may be")
    print("formatted with commas/parentheses for negatives, e.g. (3,968)).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sanity-check an XBRL fact against the parsed filing text"
    )
    parser.add_argument("--ticker", type=str, required=True, help="Ticker symbol, e.g. AAPL")
    parser.add_argument(
        "--concept", type=str, default="net_income",
        help="Concept name from fetch_xbrl.py's CONCEPTS dict, e.g. net_income"
    )
    parser.add_argument(
        "--form", type=str, default=None,
        help="Restrict to a specific form type, e.g. 10-Q or 10-K"
    )
    parser.add_argument(
        "--list-concepts", action="store_true",
        help="List available concepts for this ticker and exit"
    )
    args = parser.parse_args()

    run(
        ticker=args.ticker,
        concept=args.concept,
        form=args.form,
        list_concepts=args.list_concepts,
    )