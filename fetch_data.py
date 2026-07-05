import argparse
import json
import time
from pathlib import Path
from typing import List, Dict, Optional

import requests


HEADERS = {
    # Use your own name/app + real contact email.
    # SEC asks automated tools to declare a user agent.
    "User-Agent": "FilingLens dobaminhan@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

BASE_SEC = "https://www.sec.gov"
BASE_DATA = "https://data.sec.gov"


def sec_get_json(url: str) -> dict:
    """
    Helper function for SEC JSON requests.
    Includes a small sleep to avoid aggressive request rates.
    """
    time.sleep(0.15)  # ~6-7 requests/sec max, below SEC's 10/sec limit
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def sec_get_text(url: str) -> str:
    """
    Helper function for SEC HTML/text requests.
    """
    time.sleep(0.15)
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


# ------------------------------------------------------------
# 1. Get ticker -> CIK
# ------------------------------------------------------------

def get_company_from_ticker(ticker: str) -> Dict:
    """
    Input:
        ticker = "AAPL"

    Output:
        {
            "ticker": "AAPL",
            "cik": "0000320193",
            "company_name": "Apple Inc."
        }
    """
    ticker = ticker.upper()

    url = f"{BASE_SEC}/files/company_tickers.json"
    data = sec_get_json(url)

    for _, company in data.items():
        if company["ticker"].upper() == ticker:
            cik_10_digit = str(company["cik_str"]).zfill(10)

            return {
                "ticker": company["ticker"].upper(),
                "cik": cik_10_digit,
                "company_name": company["title"],
            }

    raise ValueError(f"Could not find ticker: {ticker}")


# ------------------------------------------------------------
# 2. Get company filing submissions
# ------------------------------------------------------------

def get_company_submissions(cik: str) -> Dict:
    """
    Uses the SEC submissions API.

    Example endpoint:
    https://data.sec.gov/submissions/CIK0000320193.json
    """
    url = f"{BASE_DATA}/submissions/CIK{cik}.json"
    return sec_get_json(url)


# ------------------------------------------------------------
# 3. Extract filing metadata
# ------------------------------------------------------------

def get_recent_filings(
    submissions: Dict,
    forms: List[str] = ["10-K", "10-Q"],
    limit: int = 5
) -> List[Dict]:
    """
    Extract recent 10-K / 10-Q filings from the submissions JSON.

    Important metadata:
        - form
        - filing_date
        - report_date
        - accession_number
        - primary_document
    """
    recent = submissions["filings"]["recent"]

    filings = []

    for i, form in enumerate(recent["form"]):
        if form in forms:
            accession_number = recent["accessionNumber"][i]
            accession_no_dashes = accession_number.replace("-", "")

            filing = {
                "form": form,
                "filing_date": recent["filingDate"][i],
                "report_date": recent["reportDate"][i],
                "accession_number": accession_number,
                "accession_no_dashes": accession_no_dashes,
                "primary_document": recent["primaryDocument"][i],
                "primary_doc_description": recent["primaryDocDescription"][i],
            }

            filings.append(filing)

        if len(filings) >= limit:
            break

    return filings


# ------------------------------------------------------------
# 4. Build actual filing URLs
# ------------------------------------------------------------

def build_filing_urls(cik: str, filing: Dict) -> Dict:
    """
    Builds useful SEC archive URLs.

    SEC archive path generally looks like:

    https://www.sec.gov/Archives/edgar/data/{CIK without leading zeros}/{accession without dashes}/{primary document}
    """
    cik_no_leading_zeros = str(int(cik))

    accession_number = filing["accession_number"]
    accession_no_dashes = filing["accession_no_dashes"]
    primary_document = filing["primary_document"]

    archive_base = (
        f"{BASE_SEC}/Archives/edgar/data/"
        f"{cik_no_leading_zeros}/"
        f"{accession_no_dashes}"
    )

    primary_document_url = f"{archive_base}/{primary_document}"

    complete_submission_text_url = f"{archive_base}/{accession_number}.txt"

    filing_detail_page_url = (
        f"{BASE_SEC}/Archives/edgar/data/"
        f"{cik_no_leading_zeros}/"
        f"{accession_number}-index.html"
    )

    return {
        "archive_base": archive_base,
        "primary_document_url": primary_document_url,
        "complete_submission_text_url": complete_submission_text_url,
        "filing_detail_page_url": filing_detail_page_url,
    }


# ------------------------------------------------------------
# 5. Put everything together
# ------------------------------------------------------------

def get_filing_records_for_ticker(
    ticker: str,
    forms: List[str] = ["10-K", "10-Q"],
    limit: int = 5
) -> List[Dict]:
    """
    This obtains all 4 things:
        1. ticker
        2. CIK
        3. filing metadata
        4. actual filing URLs
    """
    company = get_company_from_ticker(ticker)
    cik = company["cik"]

    submissions = get_company_submissions(cik)

    filings = get_recent_filings(
        submissions=submissions,
        forms=forms,
        limit=limit
    )

    final_records = []

    for filing in filings:
        urls = build_filing_urls(cik, filing)

        record = {
            "ticker": company["ticker"],
            "company_name": company["company_name"],
            "cik": cik,
            **filing,
            **urls,
        }

        final_records.append(record)

    return final_records


# ------------------------------------------------------------
# 6. Download the actual filing HTML
# ------------------------------------------------------------

def download_filing_html(record: Dict, output_dir: str = "data/filings") -> Path:
    """
    Downloads the actual 10-K / 10-Q HTML file.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    filename = (
        f"{record['ticker']}_"
        f"{record['form']}_"
        f"{record['filing_date']}_"
        f"{record['accession_no_dashes']}.html"
    )

    filepath = output_path / filename

    html = sec_get_text(record["primary_document_url"])

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    return filepath


# ------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch SEC 10-K/10-Q filing metadata and HTML for a ticker"
    )
    parser.add_argument(
        "--ticker", type=str, required=True,
        help="Ticker symbol, e.g. AAPL, MSFT, GOOGL"
    )
    parser.add_argument(
        "--forms", nargs="+", default=["10-K", "10-Q"],
        help="Form types to include (default: 10-K 10-Q)"
    )
    parser.add_argument(
        "--limit", type=int, default=3,
        help="Max number of filings to list (default: 3)"
    )
    parser.add_argument(
        "--download-index", type=int, default=0,
        help="Index into the returned filings list to download (default: 0, i.e. most recent)"
    )
    parser.add_argument(
        "--no-download", action="store_true",
        help="Only list filings, skip downloading the HTML"
    )
    args = parser.parse_args()

    records = get_filing_records_for_ticker(
        args.ticker, forms=args.forms, limit=args.limit
    )

    print(f"\nFound {len(records)} filings for {args.ticker.upper()}:\n")

    for r in records:
        print(json.dumps(r, indent=2))
        print()

    if args.no_download:
        return

    if not records:
        print("No filings found -- nothing to download.")
        return

    if args.download_index >= len(records):
        print(
            f"--download-index {args.download_index} out of range "
            f"(only {len(records)} filings found). Skipping download."
        )
        return

    target = records[args.download_index]
    print(f"\nDownloading filing HTML (index {args.download_index}): "
          f"{target['form']} filed {target['filing_date']}...\n")

    saved_path = download_filing_html(target)

    print(f"Saved to: {saved_path}")


if __name__ == "__main__":
    main()