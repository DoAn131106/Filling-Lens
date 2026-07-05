import argparse
import warnings
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import pandas as pd
import re

# Silence the XMLParsedAsHTMLWarning noise -- SEC filings are technically
# XBRL/XML-flavored HTML and lxml's HTML parser handles them fine for our
# purposes (text + table extraction), so this warning is expected noise,
# not a real problem.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

PROJECT_ROOT = Path(__file__).resolve().parent

RAW_FILING_DIR = PROJECT_ROOT / "data" / "filings"
PARSED_TEXT_DIR = PROJECT_ROOT / "data" / "parsed" / "text"
PARSED_TABLE_DIR = PROJECT_ROOT / "data" / "parsed" / "tables"

PARSED_TEXT_DIR.mkdir(parents=True, exist_ok=True)
PARSED_TABLE_DIR.mkdir(parents=True, exist_ok=True)


def clean_whitespace(text: str) -> str:
    """
    Remove excessive whitespace from SEC filing text.
    """
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_text_from_html(html_path: Path) -> str:
    """
    Extract readable text from the filing HTML.
    """
    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()

    soup = BeautifulSoup(html, "lxml")

    # Remove scripts/styles because they are not useful filing text
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")

    lines = []

    for line in text.splitlines():
        line = clean_whitespace(line)
        if line:
            lines.append(line)

    return "\n".join(lines)


def extract_tables_from_html(html_path: Path):
    """
    Extract all HTML tables from the filing.
    Returns a list of pandas DataFrames.
    """
    try:
        tables = pd.read_html(html_path)
        return tables
    except ValueError:
        return []


def save_text(text: str, output_name: str):
    output_path = PARSED_TEXT_DIR / f"{output_name}.txt"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)

    return output_path


def save_tables(tables, output_name: str):
    saved_paths = []

    for i, table in enumerate(tables):
        # Drop tables that are basically empty
        if table.empty:
            continue

        output_path = PARSED_TABLE_DIR / f"{output_name}_table_{i}.csv"
        table.to_csv(output_path, index=False)
        saved_paths.append(output_path)

    return saved_paths


def parse_single_filing(html_path: Path):
    print(f"Parsing: {html_path.name}")

    output_name = html_path.stem

    text = extract_text_from_html(html_path)
    text_path = save_text(text, output_name)

    print(f"Saved text to: {text_path}")
    print(f"Text length: {len(text):,} characters")

    tables = extract_tables_from_html(html_path)
    table_paths = save_tables(tables, output_name)

    print(f"Found {len(tables)} tables")
    print(f"Saved {len(table_paths)} table CSV files")

    for path in table_paths[:5]:
        print(f" - {path}")

    if len(table_paths) > 5:
        print(f" ... and {len(table_paths) - 5} more")

    print()


def find_html_files(ticker: str | None) -> list[Path]:
    """
    Find HTML filings in data/filings/.
    If a ticker is given, only match files starting with '{TICKER}_'
    (files are named like 'AAPL_10-Q_2026-05-01_000032019326000013.html'),
    so parsing GOOGL never accidentally picks up an AAPL file.
    """
    if ticker:
        pattern = f"{ticker.upper()}_*.html"
    else:
        pattern = "*.html"

    return sorted(RAW_FILING_DIR.glob(pattern))


def main():
    parser = argparse.ArgumentParser(
        description="Parse downloaded SEC filing HTML into text + table CSVs"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--ticker", type=str,
        help="Only parse HTML files for this ticker, e.g. GOOGL "
             "(matches files named '{TICKER}_*.html' in data/filings/)"
    )
    group.add_argument(
        "--file", type=str,
        help="Parse a specific HTML file by path"
    )
    group.add_argument(
        "--all", action="store_true",
        help="Parse every HTML file found in data/filings/"
    )
    args = parser.parse_args()

    if args.file:
        html_files = [Path(args.file)]
        if not html_files[0].exists():
            print(f"File not found: {html_files[0]}")
            return
    else:
        html_files = find_html_files(args.ticker if not args.all else None)

    if not html_files:
        scope = f" for ticker '{args.ticker.upper()}'" if args.ticker else ""
        print(f"No HTML filings found in data/filings/{scope}")
        print("Run fetch_data.py --ticker <TICKER> first.")
        return

    if args.all or args.ticker:
        print(f"Found {len(html_files)} file(s) to parse.\n")
        for html_path in html_files:
            parse_single_filing(html_path)
    else:
        # No flags given at all: default to most-recently-downloaded file,
        # not glob's arbitrary [0], and say so explicitly.
        most_recent = max(html_files, key=lambda p: p.stat().st_mtime)
        print(
            f"No --ticker/--file/--all given -- defaulting to most "
            f"recently downloaded file: {most_recent.name}\n"
        )
        parse_single_filing(most_recent)


if __name__ == "__main__":
    main()