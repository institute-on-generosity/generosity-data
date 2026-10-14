# generosity-data

Shared IRS nonprofit data pipeline and Postgres + pgvector schema for [NomBot](https://github.com/institute-on-generosity/NomBot) and [GrantRadar](https://github.com/institute-on-generosity/GrantRadar).

**Brief:** [`docs/shared-database-brief.html`](docs/shared-database-brief.html): what's loaded, what's left out, and the cloud plan (Oct 14 meeting with Philanthro Map). Download and open it in a browser; GitHub shows HTML as source.

## Local setup (proof of concept)

```bash
brew install postgresql@17 pgvector && brew services start postgresql@17
export PATH=/opt/homebrew/opt/postgresql@17/bin:$PATH
createdb nombot
db/migrate.sh                     # apply db/migrations/*.sql once each
etl/load_bmf.sh                   # IRS BMF for WV, KY, TN, VA, OH (~205K orgs, ~10s)
STATES="ca ny" etl/load_bmf.sh    # any other states; safe to re-run (upsert)
STATES=all etl/load_bmf.sh        # every state, DC and Puerto Rico (~1.97M orgs)
python3 etl/load_soi.py 24        # SOI 2024 financials for loaded orgs (~8s)
python3 etl/load_regions.py       # ZIP -> county + Appalachia (ARC) flag (~6s)
etl/fetch_990_xml.sh              # every 2025 XML batch, extract filings of loaded orgs (national: 513,813)
python3 etl/load_990_text.py      # mission + program text into filing_text (~8s)
python3 etl/load_990_flag.py      # FLAG diligence fields into filing_flag (~1 min)
python3 etl/load_county_need.py   # county poverty, income, population (Census SAIPE + estimates; no API key, ~5s)
cd embed && npm install && node embed.mjs   # 512-dim embeddings; local fallback if no OPENAI_API_KEY (CPU, ~7 rows/s)
.venv/bin/python embed/embed_gpu.py         # same local vectors on the Mac's GPU (national: ~2 h; setup in the file)
for y in 2025 2024 2023; do YEAR=$y TYPES=990PF etl/fetch_990_xml.sh; done  # every US foundation's 990-PF, 3 years (~13 GB of zips)
python3 etl/load_grants.py        # GrantRadar: funders + grants (Part XV), recipients linked to orgs (~25 min)
python3 etl/load_grants.py --match-only   # relink recipients only (e.g. after loading more states; national ~1.5 h on 8 workers)
python3 etl/load_board.py         # board members (990 Part VII, 990-PF officers) + board interlocks (~15 min)
psql nombot -f db/refresh.sql      # rebuild the newest-record views, vector index, interlocks after any load (~5 min)
```

No Python packages needed (stdlib + `psql`), except the optional GPU embedder (`.venv`: torch, sentence-transformers) and the analytics tools (`.venv-analytics`: Splink, igraph, BERTopic, DuckDB, dbt).

Claude Code reads the database through a read-only login: `nombot_reader` (read-only transactions, 60 s timeout) behind the `nombot-db` MCP server (`claude mcp add nombot-db -s user -- npx -y @modelcontextprotocol/server-postgres postgresql://nombot_reader@localhost/nombot`). Embeddings run in Node because they share code with the NomBot app's query embedder.

Set `DATABASE_URL` to target another database (e.g. Supabase after migration). Defaults to `postgresql:///nombot`.

## Tables

| Table | Source | Contents |
|---|---|---|
| `orgs` | [IRS EO BMF](https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf) | One row per exempt org; terminating orgs (status 25) skipped; keyword index on name + city |
| `financials` | [IRS SOI extract](https://www.irs.gov/statistics/soi-tax-stats-annual-extract-of-tax-exempt-organization-financial-data) | Revenue, expenses, assets by tax year and form (990, 990-EZ) |
| `filing_text` | [IRS 990 e-file XML](https://www.irs.gov/charities-non-profits/form-990-series-downloads) | Mission + program text, staff + volunteer counts (990 Part I lines 5–6; not on 990-EZ), website (header; placeholders dropped; 299,314 filings), 512-dim embedding, and the `embedding_model` that produced it |
| `filing_flag` | IRS 990 e-file XML | FLAG diligence per filing: this year vs prior year (Part I), cash, assets, liabilities, net assets (Part X), program/management/fundraising spending (Part IX), board size and independence, audit and policies (Part VI), insider flags (Part IV), officers, directors and key employees with pay (Part VII; 990-EZ Part IV). 50,971 filings |
| `county_need` | [Census SAIPE 2023](https://www.census.gov/programs-surveys/saipe.html) + [population estimates 2024](https://www.census.gov/programs-surveys/popest.html) | Per county (FIPS, joins `zip_regions.county_fips`): poverty rate (all ages, children) with its 90% range and people in poverty, median household income, population. 3,144 counties, plus the U.S. as `00000` |
| `zip_regions` | [Census ZCTA-county](https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/) + [ARC counties](https://en.wikipedia.org/wiki/List_of_Appalachian_Regional_Commission_counties) | ZIP → county (largest land share; PO-box ZIPs from same-city orgs) and whether it's in Appalachia |
| `soi_fields` | [IRS SOI field dictionary](https://www.irs.gov/pub/irs-soi/24eofinextractdoc.xlsx) | Description, form location and code meanings for every SOI extract column; `financials.raw` keeps each record's non-empty fields |
| `funders` | IRS 990-PF e-file XML | One row per private foundation in the US (newest return): assets, grants paid, invitation-only flag, how to apply (Part XV 2a–d). Donation pass-throughs (50,000+ grants on one return, e.g. AmazonSmile) are skipped |
| `grants` | IRS 990-PF Part XV 3a | Each grant paid, every loaded year: recipient name/address, amount, purpose, `tax_year`, `object_id` of its return (one return per foundation per year, the newest filed); `recipient_ein` links to `orgs` with `match_score` + `match_method` (exact, exact-city, fuzzy) |
| `board_members` | 990 Part VII, 990-PF officers | One row per person per org (each org's newest return): name, `person_key` (first + last), title, hours, pay; state on 990-PF |
| `board_interlocks` (view) | `board_members` + `grants` | Same-state org pairs sharing board members: `kind` (funder-grantee, funder-funder, funder-nonprofit, nonprofit-nonprofit), `relation` (link, sister, bank-trust), `confidence`, shared people, grants paid between them |
| `giving_circles` (view) | IRS master file + SOI | Giving circles that are their own nonprofit, found by name: Impact 100, 100 Who Care, women's funds (only those paying $10K+ in grants), "giving circle"; grants paid (990 Part IX line 1), website. 145 in 38 states. Hosted circles don't file, so they're missing |
| `feedback` | NomBot users | 👍/👎 per (question, org) from the results page; human labels for NomBot's search eval |

## Status
- [x] Schema + migration runner
- [x] BMF loader (5 Appalachian states: 204,564 orgs)
- [x] SOI financials loader (2024 extract: 54,590 rows)
- [x] 990 XML text extraction (2025 batches 05A + 11B: 12,354 filings)
- [x] FLAG diligence fields from 990 XML (migration 009: 50,971 filings)
- [x] County need from the Census (migrations 010–011: 3,144 counties + U.S.)
- [x] Embedding job + HNSW index (local fallback model until an OpenAI key is provided)
- [x] ZIP → county + Appalachia lookup (34,236 ZIPs)
- [x] Org websites from the 990 header (migration 012: 28,843 filings)
- [x] Giving circles view (migration 013: 15 circles in 5 states)
- [x] GrantRadar grants (2025 990-PFs: 8,030 foundations, 102,725 grants; 51% of in-state grants linked to a recipient)
- [x] Every US foundation, 3 IRS years (migration 014: 2023–2025 990-PFs, 135,407 foundations, 4.84M grants, tax years mostly 2022–2024; 56% of grants into the 5 states linked; 24,510 foundations fund a linked org there)
- [x] **Every US nonprofit (Oct 13)**: 1,972,852 orgs, 538,346 SOI rows, 513,813 filings (text, FLAG fields, websites), all re-embedded at full precision on the GPU; 57% of 4.84M grants linked (82,671 foundations fund a linked org)
- [x] Newest-record views + vector index on them (migration 015, `db/refresh.sql`); `btree_gin` for state-scoped fuzzy matching (016); funder × purpose sums for grant types (017)
- [x] **IRS 2026 batches (Oct 14)**: 785,772 filings for 541,879 orgs (452,462 websites); 138,734 foundations, 5,348,919 grants over 4 IRS years (tax years mostly 2022–2025, $392B); 56% linked (85,127 foundations fund a linked org; 49% of linked grants stay in the funder's state)
- [x] **Board interlocks (migration 018, `etl/load_board.py`)**: 4,859,408 board seats at 679,042 orgs; 977,088 same-state org pairs sharing a board member: 858,688 hidden links (45,116 high confidence), the rest sister orgs or bank trusts; 13,164 pairs where a foundation pays an org it shares board members with (9,213 hidden)

## Gotchas
- 990 XML batches use **Deflate64**: Python's `zipfile` can't read them; `unzip` can.
- The IRS index lists some filings under the wrong batch, and some zips are cut short (2025 05A lists 81,770 files); the fetch script matches each zip's own file list to the index, so only filings missing from every zip are skipped. 2023's index names no batches.
- Matching recipients normalizes each name once (`match_gs`) and writes links back by grant id; recomputing names inside the final update took hours on 5M grants.
- Local-fallback and OpenAI vectors are not comparable; `embedding_model` lets a provider switch re-embed only stale rows.
- The 8-bit (q8) nomic model scored only ~0.975 cosine against full precision, so the local model id is `local:nomic-embed-text-v1.5-fp32@512` and queries must use full precision too.
- `embed_gpu.py` runs out of GPU memory on the longest texts late in a run; it saves every 4,096 rows, so just rerun it (it skips finished rows).
- Fuzzy matching nationally: one session took days (each lookup searched all 2M orgs). A `(state, trigrams)` GIN index (`btree_gin`) and 8 workers on name hashes brought it to ~1.5 h.
- Board interlocks match people by first + last name within a state (`person_key()`), skipping names on more than 8 boards there; one shared name alone is low confidence. Nonprofit boards come from `filing_flag.people`, which keeps the 25 best-paid people per filing, so very large boards are cut short.
- `db/refresh.sql` uses a plain refresh with 4 GB `maintenance_work_mem`: CONCURRENTLY rewrote the vector index row by row and ran 40+ min without finishing; plain took 2.5 min.
