# generosity-data

Shared IRS nonprofit data pipeline and Postgres + pgvector schema for [NomBot](https://github.com/institute-on-generosity/NomBot) and [GrantRadar](https://github.com/institute-on-generosity/GrantRadar).

## Local setup (proof of concept)

```bash
brew install postgresql@17 pgvector && brew services start postgresql@17
export PATH=/opt/homebrew/opt/postgresql@17/bin:$PATH
createdb nombot
db/migrate.sh                     # apply db/migrations/*.sql once each
etl/load_bmf.sh                   # IRS BMF for WV, KY, TN, VA, OH (~205K orgs, ~10s)
STATES="ca ny" etl/load_bmf.sh    # any other states; safe to re-run (upsert)
python3 etl/load_soi.py 24        # SOI 2024 financials for loaded orgs (~8s)
python3 etl/load_regions.py       # ZIP -> county + Appalachia (ARC) flag (~6s)
etl/fetch_990_xml.sh              # 2025 XML batches 05A + 11B (~1 GB download), extract matching filings
python3 etl/load_990_text.py      # mission + program text into filing_text (~8s)
cd embed && npm install && node embed.mjs   # 512-dim embeddings; local fallback if no OPENAI_API_KEY
TYPES=990PF BATCHES="01A 02A 03A 04A 05A 06A 07A 08A 09A 10A 11A 11B 11C 11D 12A" etl/fetch_990_xml.sh  # foundations' 990-PFs
python3 etl/load_grants.py        # GrantRadar: funders + grants (Part XV), recipients linked to orgs (~3.5 min)
```

No Python packages needed (stdlib + `psql`). Embeddings run in Node because they share code with the NomBot app's query embedder.

Set `DATABASE_URL` to target another database (e.g. Supabase after migration). Defaults to `postgresql:///nombot`.

## Tables

| Table | Source | Contents |
|---|---|---|
| `orgs` | [IRS EO BMF](https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf) | One row per exempt org; terminating orgs (status 25) skipped; keyword index on name + city |
| `financials` | [IRS SOI extract](https://www.irs.gov/statistics/soi-tax-stats-annual-extract-of-tax-exempt-organization-financial-data) | Revenue, expenses, assets by tax year and form (990, 990-EZ) |
| `filing_text` | [IRS 990 e-file XML](https://www.irs.gov/charities-non-profits/form-990-series-downloads) | Mission + program text, 512-dim embedding, and the `embedding_model` that produced it |
| `zip_regions` | [Census ZCTA-county](https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/) + [ARC counties](https://en.wikipedia.org/wiki/List_of_Appalachian_Regional_Commission_counties) | ZIP → county (largest land share; PO-box ZIPs from same-city orgs) and whether it's in Appalachia |
| `soi_fields` | [IRS SOI field dictionary](https://www.irs.gov/pub/irs-soi/24eofinextractdoc.xlsx) | Description, form location and code meanings for every SOI extract column; `financials.raw` keeps each record's non-empty fields |
| `funders` | IRS 990-PF e-file XML | One row per private foundation (latest return): assets, grants paid, invitation-only flag, how to apply (Part XV 2a–d) |
| `grants` | IRS 990-PF Part XV 3a | Each grant paid: recipient name/address, amount, purpose; `recipient_ein` links to `orgs` with `match_score` + `match_method` (exact, exact-city, fuzzy) |
| `feedback` | NomBot users | 👍/👎 per (question, org) from the results page; human labels for NomBot's search eval |

## Status
- [x] Schema + migration runner
- [x] BMF loader (5 Appalachian states: 204,564 orgs)
- [x] SOI financials loader (2024 extract: 54,590 rows)
- [x] 990 XML text extraction (2025 batches 05A + 11B: 12,354 filings)
- [x] Embedding job + HNSW index (local fallback model until an OpenAI key is provided)
- [x] ZIP → county + Appalachia lookup (34,236 ZIPs)
- [x] GrantRadar grants (2025 990-PFs: 8,030 foundations, 102,725 grants; 51% of in-state grants linked to a recipient)

## Gotchas
- 990 XML batches use **Deflate64**: Python's `zipfile` can't read them; `unzip` can.
- The IRS index lists some filings under the wrong batch (6,219 of 05A's were absent); they're skipped.
- Local-fallback and OpenAI vectors are not comparable; `embedding_model` lets a provider switch re-embed only stale rows.
