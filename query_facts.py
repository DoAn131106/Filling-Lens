"""
query_facts.py

Thin query layer over the xbrl_facts table. This is the function your
future router/verification agents will call for any numeric question --
a direct SQL lookup, no LLM or embedding search involved.

Usage as a script (for manual testing):
    python query_facts.py --ticker AAPL --concept net_income
    python query_facts.py --ticker AAPL --concept net_income --fiscal-period Q2 --fiscal-year 2026
    python query_facts.py --compare AAPL MSFT --concept research_and_development_expense

Usage as a module (how agents will eventually use it):
    from query_facts import get_fact, get_fact_history, compare_concept
    get_fact("AAPL", "net_income")
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set. Copy .env.example to .env and fill it in.")

engine = create_engine(DATABASE_URL)


def get_fact(ticker: str, concept: str, fiscal_year: int = None, fiscal_period: str = None, form: str = None) -> dict | None:
    """
    Return the most recent matching fact for a ticker/concept, optionally
    filtered to a specific fiscal year/period/form. Returns None if no match.

    This is the function a future agent tool-call would wrap directly.
    """
    query = """
        SELECT ticker, company_name, concept, tag_used, value, unit,
               period_start, period_end, fiscal_year, fiscal_period,
               form, filed_date, accession_number
        FROM xbrl_facts
        WHERE ticker = :ticker AND concept = :concept
    """
    params = {"ticker": ticker.upper(), "concept": concept}

    if fiscal_year is not None:
        query += " AND fiscal_year = :fiscal_year"
        params["fiscal_year"] = fiscal_year
    if fiscal_period is not None:
        query += " AND fiscal_period = :fiscal_period"
        params["fiscal_period"] = fiscal_period
    if form is not None:
        query += " AND form = :form"
        params["form"] = form

    query += " ORDER BY period_end DESC LIMIT 1"

    with engine.connect() as conn:
        row = conn.execute(text(query), params).mappings().first()

    return dict(row) if row else None


def get_fact_history(ticker: str, concept: str, form: str = None) -> list[dict]:
    """Return the full time series for a ticker/concept, oldest to newest."""
    query = """
        SELECT ticker, concept, value, unit, period_end, fiscal_year,
               fiscal_period, form, accession_number
        FROM xbrl_facts
        WHERE ticker = :ticker AND concept = :concept
    """
    params = {"ticker": ticker.upper(), "concept": concept}

    if form is not None:
        query += " AND form = :form"
        params["form"] = form

    query += " ORDER BY period_end ASC"

    with engine.connect() as conn:
        rows = conn.execute(text(query), params).mappings().all()

    return [dict(r) for r in rows]


def compare_concept(tickers: list[str], concept: str, form: str = None) -> list[dict]:
    """Return the most recent value of a concept across multiple tickers."""
    results = []
    for ticker in tickers:
        fact = get_fact(ticker, concept, form=form)
        if fact:
            results.append(fact)
    return results


def _print_fact(fact: dict | None):
    if not fact:
        print("  No matching fact found.")
        return
    print(f"  {fact['ticker']} ({fact['company_name']})")
    print(f"  {fact['concept']}: {fact['value']:,} {fact['unit']}")
    print(f"  Period end: {fact['period_end']} | {fact['fiscal_period']} {fact['fiscal_year']} | {fact['form']}")
    print(f"  Accession: {fact['accession_number']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query the xbrl_facts table directly")
    parser.add_argument("--ticker", type=str, help="Single ticker to look up")
    parser.add_argument("--concept", type=str, required=True, help="Concept name, e.g. net_income")
    parser.add_argument("--fiscal-year", type=int, default=None)
    parser.add_argument("--fiscal-period", type=str, default=None, help="e.g. Q1, Q2, FY")
    parser.add_argument("--form", type=str, default=None, help="e.g. 10-Q, 10-K")
    parser.add_argument("--history", action="store_true", help="Show full time series instead of just latest")
    parser.add_argument("--compare", nargs="+", default=None, help="Compare this concept across multiple tickers")
    args = parser.parse_args()

    if args.compare:
        print(f"Comparing '{args.concept}' across {args.compare}:\n")
        results = compare_concept(args.compare, args.concept, form=args.form)
        for fact in results:
            _print_fact(fact)
            print()
    elif args.history:
        if not args.ticker:
            parser.error("--history requires --ticker")
        print(f"History for {args.ticker} / {args.concept}:\n")
        rows = get_fact_history(args.ticker, args.concept, form=args.form)
        for r in rows:
            print(f"  {r['period_end']} ({r['fiscal_period']} {r['fiscal_year']}, {r['form']}): "
                  f"{r['value']:,} {r['unit']}")
    else:
        if not args.ticker:
            parser.error("Provide --ticker, or use --compare with multiple tickers")
        fact = get_fact(args.ticker, args.concept, args.fiscal_year, args.fiscal_period, args.form)
        _print_fact(fact)