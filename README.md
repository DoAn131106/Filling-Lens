# FilingLens

**Multi-agent financial filing intelligence for SEC 10-K/10-Q filings.**

FilingLens ingests SEC filings and grounds every answer to a financial question in the actual source — structured XBRL data for hard numbers, parsed filing text for narrative context — instead of letting an LLM guess or hallucinate figures. It's built to answer questions like:

- *What was Apple's net income last quarter?*
- *How much did R&D expense grow year-over-year?*
- *What was gross margin, and how does it compare across companies?*

## Why this exists

Most RAG-over-documents demos fail quietly on financial filings: numbers get mangled by naive HTML table parsing, embeddings can't reliably distinguish "Q2 2026" from "Q2 2025," and nothing verifies that a generated answer's numbers actually match the source. FilingLens is built around one core design decision: **use SEC's structured XBRL data as ground truth for numeric facts, and reserve narrative text retrieval for context and explanation** — rather than trying to force every question through a single retrieval path.

## Current status: 🚧 in progress

This is an active build. What exists today:

### ✅ Ingestion pipeline
- `fetch_data.py` — resolves ticker → CIK via SEC's `company_tickers.json`, pulls recent 10-K/10-Q metadata via the SEC submissions API, and downloads filing HTML
- `parse_filing.py` — extracts clean text and HTML tables from downloaded filings

### ✅ Structured financial data layer
- `fetch_xbrl.py` — pulls structured XBRL `companyfacts` data per company (revenue, net income, R&D expense, gross margin, and more), with tag-fallback logic to handle inconsistent XBRL tagging across filers
- `fetch_xbrl_batch.py` — runs the XBRL fetch across multiple tickers for cross-company comparison

### ✅ Validation & eval foundation
- `data_reconciliation.py` — cross-checks every extracted XBRL value against the actual filing text, so numeric errors are caught before they propagate downstream
- `eval/golden_facts.json` + `add_golden_fact.py` — a growing hand-verified dataset of (concept, value, source) records, seeding the numeric-accuracy eval this project is built to take seriously

### 🔜 Coming next
- Structured Postgres store for XBRL facts (`pgvector` for narrative text embeddings)
- Multi-agent orchestration (LangGraph) — router, retrieval, analyst, and **verification** agents, where every generated numeric claim is checked against the structured fact store before an answer ships
- Visual document retrieval for tables/exhibits that don't parse cleanly as HTML or XBRL
- Full RAGAS-based eval harness with a custom numeric-accuracy metric, wired into CI
- LLMOps: tracing, cost/latency monitoring, prompt versioning

## Design principles

1. **Structured data first.** If a fact is tagged in XBRL, extract it from there — not from a scraped HTML table.
2. **Every number is verifiable.** Any figure the system reports traces back to a specific filing, accession number, and (once agents are built) passes a verification step before being shown to a user.
3. **Reconciliation isn't optional.** Every new concept/company combination gets manually spot-checked against the source filing before being trusted.

## Tech stack (current + planned)

`Python` · `requests` · `pandas` · `BeautifulSoup` · SEC EDGAR REST + XBRL APIs · *(planned: LangGraph, pgvector, Postgres, RAGAS, Langfuse)*

## Data source

All data comes directly from [SEC EDGAR](https://www.sec.gov/edgar) — the submissions API, XBRL company facts API, and raw filing archives. No third-party paid API is used. Requests are rate-limited and identify themselves via a descriptive `User-Agent`, per SEC's fair access guidelines.

---

*This project is a personal portfolio build exploring production-grade RAG and multi-agent system design applied to financial filings. Not affiliated with the SEC or any company whose filings are referenced.*