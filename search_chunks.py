"""
search_chunks.py

Semantic search over the filing_chunks table: takes a natural-language
question, embeds it with the same OpenAI model used for the stored
chunks, and finds the closest matches via pgvector cosine distance.

This is the retrieval half of RAG -- the narrative-text counterpart to
query_facts.py's exact SQL lookups on the structured XBRL side. Later,
a router agent decides which of these two tools to call based on the
question type (numeric fact vs. explanatory/narrative question).

Usage:
    python3 search_chunks.py --query "why did R&D expense increase?"
    python3 search_chunks.py --query "risk factors related to supply chain" --ticker AAPL
    python3 search_chunks.py --query "gross margin trends" --ticker AAPL MSFT --top-k 3
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set. Copy .env.example to .env and fill it in.")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set. Add it to your .env file.")

# Must match the model used in chunk_and_embed_filing.py -- mixing
# embedding models would give meaningless similarity scores, since
# different models produce vectors in different, incompatible spaces.
EMBEDDING_MODEL = "text-embedding-3-small"

engine = create_engine(DATABASE_URL)
client = OpenAI(api_key=OPENAI_API_KEY)


def embed_query(query: str) -> list[float]:
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=[query])
    return response.data[0].embedding


def search(
    query: str,
    tickers: list[str] | None = None,
    form: str | None = None,
    section_contains: str | None = None,
    top_k: int = 5,
) -> list[dict]:
    """
    Return the top_k most semantically similar chunks to `query`,
    optionally filtered by ticker(s), form type, or section keyword.

    Uses pgvector's <=> operator (cosine distance -- lower is more similar).
    """
    query_embedding = embed_query(query)
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    sql = """
        SELECT ticker, company_name, form, fiscal_year, fiscal_period,
               section, chunk_index, chunk_text, accession_number,
               embedding <=> CAST(:embedding AS vector) AS distance
        FROM filing_chunks
        WHERE 1=1
    """
    params = {"embedding": embedding_str, "top_k": top_k}

    if tickers:
        sql += " AND ticker = ANY(:tickers)"
        params["tickers"] = [t.upper() for t in tickers]

    if form:
        sql += " AND form = :form"
        params["form"] = form

    if section_contains:
        sql += " AND section ILIKE :section_pattern"
        params["section_pattern"] = f"%{section_contains}%"

    sql += " ORDER BY distance ASC LIMIT :top_k"

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    return [dict(r) for r in rows]


def _print_result(rank: int, result: dict):
    similarity_pct = (1 - result["distance"]) * 100  # cosine distance -> rough similarity %
    print(f"[{rank}] {result['ticker']} | {result['section'][:60]} | "
          f"{result['form']} {result['fiscal_period']} {result['fiscal_year']} | "
          f"similarity: {similarity_pct:.1f}%")
    preview = result["chunk_text"][:300].replace("\n", " ")
    print(f"    {preview}...")
    print(f"    accession: {result['accession_number']}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Semantic search over parsed filing text")
    parser.add_argument("--query", type=str, required=True, help="Natural-language question")
    parser.add_argument("--ticker", nargs="+", default=None, help="Restrict to one or more tickers")
    parser.add_argument("--form", type=str, default=None, help="Restrict to a form type, e.g. 10-Q")
    parser.add_argument("--section", type=str, default=None, help="Restrict to sections matching this keyword")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results to return (default: 5)")
    args = parser.parse_args()

    print(f"Query: \"{args.query}\"")
    if args.ticker:
        print(f"Filtered to: {args.ticker}")
    print()

    results = search(
        query=args.query,
        tickers=args.ticker,
        form=args.form,
        section_contains=args.section,
        top_k=args.top_k,
    )

    if not results:
        print("No results found.")
    else:
        for i, r in enumerate(results, start=1):
            _print_result(i, r)