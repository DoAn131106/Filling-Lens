"""
chunk_and_embed_filing.py

Takes a parsed filing text file (from data/parsed/text/), splits it into
section-aware, token-sized chunks, embeds each chunk via OpenAI's
text-embedding-3-small, and stores everything in the filing_chunks table
(pgvector) for narrative/semantic search later.

Section detection is a light heuristic: SEC filings consistently use
"Item 1.", "Item 1A.", "Item 2." etc. as headers (Item 1 = Financial
Statements, Item 1A = Risk Factors, Item 2 = MD&A, ...). We split on
those headers first, then token-chunk within each section, so every
chunk knows which section it came from.

Requires in .env:
    DATABASE_URL=postgresql+psycopg2://USER@localhost:5432/filinglens
    OPENAI_API_KEY=sk-...

Usage:
    python3 chunk_and_embed_filing.py --ticker AAPL           # all AAPL text files
    python3 chunk_and_embed_filing.py --file path/to/file.txt # one specific file
    python3 chunk_and_embed_filing.py --ticker AAPL --dry-run # preview, no API calls, no DB writes
"""

import argparse
import os
import re
from pathlib import Path

import tiktoken
from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parent
TEXT_DIR = PROJECT_ROOT / "data" / "parsed" / "text"

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set. Copy .env.example to .env and fill it in.")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set. Add it to your .env file.")

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536  # must match the VECTOR(1536) column in filing_chunks

DEFAULT_CHUNK_SIZE_TOKENS = 500
DEFAULT_OVERLAP_TOKENS = 50

# How many chunks to send to the OpenAI embeddings endpoint per API call.
# Batching keeps requests fast and cheap without hitting request-size limits.
EMBEDDING_BATCH_SIZE = 100

# Matches SEC filing section headers like "Item 1. Financial Statements",
# "Item 1A. Risk Factors", "Item 2. Management's Discussion...".
SECTION_HEADER_PATTERN = re.compile(
    r"(Item\s+\d+[A-Za-z]?\.\s*[A-Za-z][^\n]{0,100})",
    re.IGNORECASE,
)

encoding = tiktoken.encoding_for_model(EMBEDDING_MODEL)
client = OpenAI(api_key=OPENAI_API_KEY)


# ---------------------------------------------------------------------------
# Filename parsing -- recover ticker/form/accession from parse_filing.py's
# naming convention: {TICKER}_{FORM}_{FILING_DATE}_{ACCESSION_NO_DASHES}.txt
# ---------------------------------------------------------------------------

FILENAME_PATTERN = re.compile(
    r"^(?P<ticker>[A-Z]+)_(?P<form>10-[KQ](?:/A)?)_(?P<filing_date>\d{4}-\d{2}-\d{2})_(?P<accession_nodash>\d+)$"
)


def parse_filename(text_path: Path) -> dict:
    match = FILENAME_PATTERN.match(text_path.stem)
    if not match:
        raise ValueError(
            f"Filename '{text_path.name}' doesn't match expected pattern "
            f"'{{TICKER}}_{{FORM}}_{{DATE}}_{{ACCESSION_NODASH}}.txt'"
        )
    parts = match.groupdict()
    nodash = parts["accession_nodash"]
    # Reconstruct dashed accession number: 10 digits - 2 digits - 6 digits
    accession_number = f"{nodash[0:10]}-{nodash[10:12]}-{nodash[12:18]}"
    return {
        "ticker": parts["ticker"],
        "form": parts["form"],
        "filing_date": parts["filing_date"],
        "accession_number": accession_number,
    }


def get_filing_metadata(engine, ticker: str, accession_number: str) -> dict:
    """
    Pull cik/company_name/fiscal_year/fiscal_period from xbrl_facts if we
    have a matching row for this exact filing (accession number). Falls
    back to just cik/company_name (any row for the ticker) if this exact
    accession isn't in xbrl_facts, leaving fiscal_year/period as None.
    """
    with engine.connect() as conn:
        exact = conn.execute(
            text("""
                SELECT cik, company_name, fiscal_year, fiscal_period
                FROM xbrl_facts
                WHERE ticker = :ticker AND accession_number = :accession_number
                LIMIT 1
            """),
            {"ticker": ticker, "accession_number": accession_number},
        ).mappings().first()

        if exact:
            return dict(exact)

        fallback = conn.execute(
            text("SELECT cik, company_name FROM xbrl_facts WHERE ticker = :ticker LIMIT 1"),
            {"ticker": ticker},
        ).mappings().first()

    if fallback:
        return {**dict(fallback), "fiscal_year": None, "fiscal_period": None}

    return {"cik": None, "company_name": None, "fiscal_year": None, "fiscal_period": None}


# ---------------------------------------------------------------------------
# Section splitting + token-aware chunking
# ---------------------------------------------------------------------------

def merge_small_sections(sections: list[tuple[str, str]], min_tokens: int = 40) -> list[tuple[str, str]]:
    """
    Merge sections below a token threshold into the section that follows.

    This exists because SEC filings list every 'Item N.' header twice:
    once in the Table of Contents (just a title + page number, near-empty)
    and again where the real section content actually starts. Without
    this merge step, TOC entries become their own tiny, nearly-content-free
    "sections" that pollute retrieval -- a query like "risk factors" could
    rank a bare TOC stub highly on lexical similarity alone, despite it
    having nothing useful to answer with.

    Any trailing small section at the end of the document (no next section
    to merge into) gets folded into the previous section instead.
    """
    merged: list[tuple[str, str]] = []
    buffer_title = None
    buffer_text = ""

    for title, section_text in sections:
        combined_text = f"{buffer_text}\n{section_text}" if buffer_text else section_text
        token_len = len(encoding.encode(combined_text))

        if token_len < min_tokens:
            buffer_title = buffer_title or title
            buffer_text = combined_text
        else:
            merged.append((buffer_title or title, combined_text))
            buffer_title = None
            buffer_text = ""

    if buffer_text:
        if merged:
            last_title, last_text = merged[-1]
            merged[-1] = (last_title, f"{last_text}\n{buffer_text}")
        else:
            merged.append((buffer_title, buffer_text))

    return merged


def normalize_header(title: str) -> str:
    """Collapse whitespace/case so the same header matches whether it's
    the TOC listing or the real section heading (formatting can differ
    slightly, e.g. line breaks)."""
    return re.sub(r"\s+", " ", title).strip().lower()


def split_into_sections(full_text: str) -> list[tuple[str, str]]:
    """
    Split filing text on 'Item N.' style headers.

    SEC filings list every 'Item N.' header TWICE: once in the Table of
    Contents (title + page number only, no real content) and again where
    the actual section content begins. A naive split-on-every-header
    approach treats the TOC line as its own tiny "section", polluting
    retrieval with content-free chunks.

    Fix: for each unique header title, only the LAST occurrence in the
    document is treated as a real section boundary -- earlier occurrences
    are assumed to be TOC references and are folded into a leading
    "Cover Page / Preamble" section instead of becoming their own chunks.

    Falls back to a single "Full Document" section if no headers are
    detected at all.
    """
    matches = list(SECTION_HEADER_PATTERN.finditer(full_text))
    if not matches:
        return [("Full Document", full_text)]

    title_positions: dict[str, list[int]] = {}
    for i, m in enumerate(matches):
        norm = normalize_header(m.group(1))
        title_positions.setdefault(norm, []).append(i)

    # Keep only the last occurrence of each unique header text.
    keep_indices = sorted(idxs[-1] for idxs in title_positions.values())
    kept_matches = [matches[i] for i in keep_indices]

    sections = []
    for i, m in enumerate(kept_matches):
        start = m.start()
        end = kept_matches[i + 1].start() if i + 1 < len(kept_matches) else len(full_text)
        title = m.group(1).strip()
        section_text = full_text[start:end]
        sections.append((title, section_text))

    # Anything before the first *real* section (cover page, TOC itself) is
    # kept as its own section rather than silently dropped.
    first_start = kept_matches[0].start()
    if first_start > 0:
        preamble = full_text[:first_start]
        sections.insert(0, ("Cover Page / Table of Contents", preamble))

    # Safety net for any remaining tiny fragments (e.g. a short exhibit
    # index or signature block that isn't part of the Item N. pattern).
    return merge_small_sections(sections)


def chunk_text_by_tokens(section_text: str, chunk_size: int, overlap: int) -> list[str]:
    """Token-aware sliding-window chunking using the model's own tokenizer."""
    tokens = encoding.encode(section_text)
    if not tokens:
        return []

    chunks = []
    start = 0
    while start < len(tokens):
        end = start + chunk_size
        chunk_tokens = tokens[start:end]
        chunks.append(encoding.decode(chunk_tokens))
        if end >= len(tokens):
            break
        start = end - overlap

    return chunks


def build_chunks(full_text: str, chunk_size: int, overlap: int) -> list[dict]:
    """Returns list of {"section": ..., "chunk_text": ...} dicts, in order."""
    chunks = []
    for section_title, section_text in split_into_sections(full_text):
        for chunk_str in chunk_text_by_tokens(section_text, chunk_size, overlap):
            chunks.append({"section": section_title, "chunk_text": chunk_str})
    return chunks


# ---------------------------------------------------------------------------
# Embedding + storage
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of strings in batches, preserving order."""
    all_embeddings = []
    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i:i + EMBEDDING_BATCH_SIZE]
        response = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        all_embeddings.extend([item.embedding for item in response.data])
    return all_embeddings


UPSERT_SQL = text("""
    INSERT INTO filing_chunks (
        cik, ticker, company_name, form, fiscal_year, fiscal_period,
        accession_number, section, chunk_index, chunk_text, embedding
    )
    VALUES (
        :cik, :ticker, :company_name, :form, :fiscal_year, :fiscal_period,
        :accession_number, :section, :chunk_index, :chunk_text,
        CAST(:embedding AS vector)
    )
    ON CONFLICT (accession_number, chunk_index)
    DO UPDATE SET
        chunk_text = EXCLUDED.chunk_text,
        section = EXCLUDED.section,
        embedding = EXCLUDED.embedding,
        cik = EXCLUDED.cik,
        ticker = EXCLUDED.ticker,
        company_name = EXCLUDED.company_name,
        form = EXCLUDED.form,
        fiscal_year = EXCLUDED.fiscal_year,
        fiscal_period = EXCLUDED.fiscal_period
""")


def process_file(engine, text_path: Path, chunk_size: int, overlap: int, dry_run: bool) -> int:
    print(f"\n{text_path.name}")

    file_info = parse_filename(text_path)
    metadata = get_filing_metadata(engine, file_info["ticker"], file_info["accession_number"])

    full_text = text_path.read_text(encoding="utf-8", errors="ignore")
    chunks = build_chunks(full_text, chunk_size, overlap)

    if not chunks:
        print("  No text to chunk, skipping.")
        return 0

    print(f"  {len(chunks)} chunks across {len(set(c['section'] for c in chunks))} section(s)")

    if dry_run:
        for c in chunks[:3]:
            preview = c["chunk_text"][:80].replace("\n", " ")
            print(f"    [{c['section'][:40]}] {preview}...")
        if len(chunks) > 3:
            print(f"    ... and {len(chunks) - 3} more chunks")
        return len(chunks)

    print(f"  Embedding {len(chunks)} chunks via {EMBEDDING_MODEL}...")
    embeddings = embed_texts([c["chunk_text"] for c in chunks])

    with engine.begin() as conn:
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
            conn.execute(UPSERT_SQL, {
                "cik": metadata["cik"],
                "ticker": file_info["ticker"],
                "company_name": metadata["company_name"],
                "form": file_info["form"],
                "fiscal_year": metadata["fiscal_year"],
                "fiscal_period": metadata["fiscal_period"],
                "accession_number": file_info["accession_number"],
                "section": chunk["section"],
                "chunk_index": i,
                "chunk_text": chunk["chunk_text"],
                "embedding": embedding_str,
            })

    print(f"  Stored {len(chunks)} chunks in filing_chunks.")
    return len(chunks)


def main():
    parser = argparse.ArgumentParser(description="Chunk and embed parsed SEC filing text into pgvector")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", type=str, help="Process all text files for this ticker, e.g. AAPL")
    group.add_argument("--file", type=str, help="Process one specific text file by path")
    group.add_argument("--all", action="store_true", help="Process every text file in data/parsed/text/")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE_TOKENS,
                         help=f"Tokens per chunk (default: {DEFAULT_CHUNK_SIZE_TOKENS})")
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP_TOKENS,
                         help=f"Overlap tokens between chunks (default: {DEFAULT_OVERLAP_TOKENS})")
    parser.add_argument("--dry-run", action="store_true",
                         help="Preview chunking without calling OpenAI or writing to the DB")
    args = parser.parse_args()

    if args.file:
        text_files = [Path(args.file)]
    elif args.ticker:
        text_files = sorted(TEXT_DIR.glob(f"{args.ticker.upper()}_*.txt"))
    else:
        text_files = sorted(TEXT_DIR.glob("*.txt"))

    if not text_files:
        print("No matching text files found in data/parsed/text/.")
        print("Run fetch_data.py + parse_filing.py first.")
        return

    engine = create_engine(DATABASE_URL)

    print(f"Found {len(text_files)} file(s) to process.")
    if args.dry_run:
        print("DRY RUN -- no OpenAI calls, no DB writes\n")

    total_chunks = 0
    for text_path in text_files:
        try:
            total_chunks += process_file(engine, text_path, args.chunk_size, args.overlap, args.dry_run)
        except Exception as e:
            print(f"  FAILED on {text_path.name}: {e}")

    print(f"\nTotal chunks {'previewed' if args.dry_run else 'stored'}: {total_chunks}")

    if not args.dry_run:
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM filing_chunks")).scalar()
            print(f"filing_chunks table now has {count} total rows.")


if __name__ == "__main__":
    main()