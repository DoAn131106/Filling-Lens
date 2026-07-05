"""
fetch_xbrl_batch.py

Loops fetch_xbrl.py's run() across multiple tickers, so you build up
a structured XBRL fact store across several companies instead of just
AAPL. Useful for enabling comparative questions later
("compare R&D spend growth to competitors").

Respects SEC rate limits: fetch_xbrl.py already sleeps between requests
within a single company's fetch; this script adds an extra pause
*between* companies as a safety margin.

Usage:
    python fetch_xbrl_batch.py --tickers AAPL MSFT GOOGL AMZN
    python fetch_xbrl_batch.py --tickers-file tickers.txt
"""

import argparse
import time
from pathlib import Path

# Reuse everything from fetch_xbrl.py rather than duplicating logic.
from fetch_xbrl import run as fetch_one_ticker

PROJECT_ROOT = Path(__file__).resolve().parent

# Extra pause between companies, on top of fetch_xbrl.py's per-request delay.
PAUSE_BETWEEN_COMPANIES_SECONDS = 1.0

# A reasonable default watchlist if you don't pass --tickers: Apple plus a
# few companies useful for comparative R&D / margin questions later.
DEFAULT_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "META"]


def load_tickers_from_file(path: Path) -> list[str]:
    with open(path) as f:
        return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]


def run_batch(tickers: list[str]):
    print(f"Fetching XBRL facts for {len(tickers)} tickers: {tickers}\n")

    results = {"succeeded": [], "failed": []}

    for i, ticker in enumerate(tickers, start=1):
        print("=" * 70)
        print(f"[{i}/{len(tickers)}] {ticker}")
        print("=" * 70)
        try:
            fetch_one_ticker(ticker=ticker, cik=None)
            results["succeeded"].append(ticker)
        except Exception as e:
            print(f"FAILED for {ticker}: {e}")
            results["failed"].append((ticker, str(e)))

        if i < len(tickers):
            time.sleep(PAUSE_BETWEEN_COMPANIES_SECONDS)
        print()

    print("=" * 70)
    print("BATCH SUMMARY")
    print("=" * 70)
    print(f"Succeeded ({len(results['succeeded'])}): {results['succeeded']}")
    if results["failed"]:
        print(f"Failed ({len(results['failed'])}):")
        for ticker, err in results["failed"]:
            print(f"  - {ticker}: {err}")
    else:
        print("Failed: none")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch XBRL facts for multiple tickers")
    parser.add_argument(
        "--tickers", nargs="+", default=None,
        help="Space-separated list of tickers, e.g. --tickers AAPL MSFT GOOGL"
    )
    parser.add_argument(
        "--tickers-file", type=str, default=None,
        help="Path to a text file with one ticker per line"
    )
    args = parser.parse_args()

    if args.tickers_file:
        tickers = load_tickers_from_file(Path(args.tickers_file))
    elif args.tickers:
        tickers = [t.upper() for t in args.tickers]
    else:
        print(f"No --tickers or --tickers-file given; using default watchlist: {DEFAULT_TICKERS}")
        tickers = DEFAULT_TICKERS

    run_batch(tickers)