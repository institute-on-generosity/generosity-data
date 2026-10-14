# generosity-data

Shared IRS nonprofit data pipeline and Postgres + pgvector schema for [NomBot](https://github.com/institute-on-generosity/NomBot) and [GrantRadar](https://github.com/institute-on-generosity/GrantRadar).

**Brief:** [read it below](#brief-one-database-for-our-990-projects): what's loaded, what's left out, data sources, the cloud plan and insights (Oct 14 meeting with Philanthro Map). Interactive version: [`docs/shared-database-brief.html`](docs/shared-database-brief.html).

**Data sources:** [`docs/data-sources.md`](docs/data-sources.md): every source, connected or coming, by what it brings, with API links.

**Health:** [`docs/db-health.md`](docs/db-health.md): checks, counts, freshness and table sizes; regenerate with `python3 etl/db_health.py` after a load.

**Graph view:** [`docs/board-interlocks/`](docs/board-interlocks/): orgs that share board members, by state. Build its data with `python3 etl/export_interlocks.py` (git-ignored: it lists board members' names), then `python3 -m http.server -d docs/board-interlocks`.

## Brief: one database for our 990 projects

Written for the Oct 14, 2026 meeting with Tyler (Philanthro Map), kept current. Same content as an interactive page: [`docs/shared-database-brief.html`](docs/shared-database-brief.html) (download and open in a browser) or the [claude.ai brief](https://claude.ai/artifact/C9ijG2YA29zEoDEM9rs4Bf) (shared on request).

| nonprofits | 990 returns | foundations | grants · 4 IRS years · $392B | counties | database |
|---|---|---|---|---|---|
| **1.97M** | **785,772** | **138,734** | **5.35M** | **3,144** | **14 GB** |

- **Largest:** 990 text + vectors 7 GB, grants 2 GB, board members 1.5 GB, orgs 1.1 GB.
- **Raw IRS files:** 58 GB (14 GB zips + 44 GB unzipped). Not stored. Re-downloadable.
- **Why smaller:** we keep ~40 of ~440 values per 990, as numbers without XML tags.

<details><summary><b>Database</b>: how data flows, what we keep and leave out, tables, vector search, limits</summary>

### How data flows

```mermaid
flowchart LR
  IRS["IRS 990s + master file"] --> ETL["Python ETL"]
  CEN["Census"] --> ETL
  ETL --> DB[("Postgres")]
  DB --> NB["NomBot"]
  DB --> GR["GrantRadar"]
  DB -.-> PM["Philanthro Map"]
```

- **ETL:** scripts that download, clean and load public files. One per source, in [etl/](https://github.com/institute-on-generosity/generosity-data/tree/main/etl).
- **Plain Python + psql.** Re-runs update rows, never duplicate.
- **Runs** by hand. Monthly on GitHub Actions after the cloud move.

### What we keep from a 990

A typical 990 fills in **~440** values (median of 254 filings; 990-EZ ~160, 990-PF ~400). We keep about 40.

| We keep | Part |
|---|---|
| **990 · ~40 fields** | |
| Mission, programs | I, III |
| Staff, volunteers, website | I |
| Revenue, expenses, gifts, pay (2 years) | I |
| Government grants | VIII |
| Cash, assets, debts | X |
| Program, admin, fundraising spend | IX |
| Board, policies, audit | VI, XII |
| Insider deals | IV |
| Officers and pay | VII |
| **990-PF · ~20 fields** | |
| Assets, total giving | I |
| Invite only, how to apply | XV 2 |
| Each grant: who, amount, purpose | XV 3a |

### What we leave out

★ = worth adding next

| Part | Not loaded |
|---|---|
| **990** | |
| III | Dollars per program |
| V | W-2 and 1099 counts, foreign accounts |
| VII B | Contractors over $100K |
| VIII | Revenue by source★ |
| IX | 24 expense lines (rent, travel, IT)★ |
| X | Balance sheet detail |
| XII | Accounting method |
| Sch. A | Charity type, public support test |
| Sch. B | Donors (blanked in public copies) |
| Sch. C | Lobbying, political spending |
| Sch. D | Endowments, donor advised funds★ |
| Sch. F | Work abroad |
| Sch. G | Fundraising events |
| Sch. H | Hospital community benefit |
| Sch. I | Grants by community foundations, DAF sponsors★ |
| Sch. J | Executive pay detail |
| Sch. L | Insider deals, itemized |
| Sch. M | Gifts of property, stock |
| Sch. O | Free-text explanations |
| Sch. R | Related orgs |
| **990-PF** | |
| I | Revenue and expense lines |
| II | Investments |
| Officers | Officers and pay |
| XV 3b | Grants approved, not yet paid★ |
| Activities | Program-related investments |

### Tables

```mermaid
erDiagram
  funders ||--o{ grants : pays
  grants }o--o| orgs : "recipient_ein"
  orgs ||--o{ filing_text : files
  orgs ||--o{ filing_flag : files
  orgs ||--o{ financials : reports
  grants }o--|| zip_regions : "recipient_zip"
  orgs }o--|| zip_regions : zip
  zip_regions }o--|| county_need : county_fips
```

| Table | Holds |
|---|---|
| `orgs` | Every nonprofit: address, cause, revenue |
| `financials` | Revenue, expenses, assets by year |
| `filing_text` | Mission, programs, staff, website |
| `filing_flag` | Cash, debts, spending, board, pay |
| `funders` | Foundations: assets, how to apply |
| `grants` | Each grant: recipient, ZIP, amount, purpose |
| `zip_regions` | ZIP → county, Appalachia |
| `county_need` | Poverty, income, population |

### Vector search

```mermaid
flowchart LR
  Q["Mission"] --> E["Embed"] --> V["HNSW search"] --> M["Merge"]
  Q --> K["Keyword search"] --> M
  M --> C["Claude scores top 40"]
```

- **Embedded:** mission + programs, 785,772 filings.
- **Model:** nomic-embed-text-v1.5, local, 512 dimensions. Cloud: OpenAI text-embedding-3-small.
- **Index:** HNSW cosine on `filing_latest`, the newest filing per org.
- **Model switch:** re-embeds only stale rows, via `embedding_model`.
- **GrantRadar:** mission → 150 closest grantees → their funders.

### For Philanthro Map

**90% of grants map to a county:** `grants.recipient_zip → zip_regions → county_need`.

- **3,196** counties receive grants.
- **52%** of grants stay in the funder's state.

### Limits

- **Text:** only orgs that e-file a 990 (541,879 of 1.97M).
- **Years:** IRS 2023 to 2026 (tax years mostly 2022 to 2025).
- **Recipients:** no EIN on grants. 56% matched by name.
- **Individuals:** not on any 990.

### Decide

1. Schema changes: migration + PR?
2. More years: before or after Supabase?
3. Until then: local copy or my dump?
4. What data is the map missing?
5. One county map and color scale for both apps?

</details>

<details><summary><b>Data sources</b>: connected, coming, keeping it fresh</summary>

Every source by what it brings, with API links and status: [`docs/data-sources.md`](docs/data-sources.md).

### Keeping it fresh

| How | Sources | When |
|---|---|---|
| **Nightly check** | IRS 990s, master file, Pub 78, revocations, Census | Every night, 2 AM |
| **Frequent poll** | FEMA hourly. USAspending, Grants.gov daily | Asks only for records changed since last time |
| **Instant write** | Claimed profiles, feedback, saved lists | The app writes straight to Supabase |
| **Fetch on open** | ProPublica org detail | When a page opens, cached 1 day |

**Nightly check:** Ask: anything new? → Load only the new records → Embed and link them → Publish all at once → Apps show it by morning.

</details>

<details><summary><b>Cloud plan</b>: now, next, ultimate; stages; how big; where embeddings run</summary>

### Now, next, ultimate

| | **Now**: Local only, $0 | **Next**: Cloud migration, ~$60 to $110 a month | **Ultimate**: Own workstation + cloud, ~$150 a month + workstation |
|---|---|---|---|
| **ETL** | Python on your Mac, by hand | GitHub Actions, monthly | Always-on workstation, nightly. More sources (Schedule I, more years) |
| **Embed** | nomic. Filings on the Mac GPU, questions in the app | Embedding API (OpenAI or Voyage) for filings and questions | Best open model from the eval. Bulk on the workstation GPU, questions on a small hosted copy of the same model |
| **Store** | Postgres on your Mac | Supabase Postgres + pgvector | Supabase + a read-only replica for the map and partners. Staging copy on the workstation |
| **Serve** | Dev servers at localhost | Vercel: NomBot, GrantRadar, public API | Vercel, with a preview site for every change |
| **Access** | Only you | Public apps and API. Map reads with a read-only role | API keys for partners, with usage limits |
| **Backups** | None | Supabase, daily | Point-in-time restore. Raw IRS archive on the workstation |

### Moving to the cloud

1. **Get** (GitHub Actions, monthly): Download public files. Uses: Raw IRS files, Census files.
2. **Clean + load** (GitHub Actions, monthly. Tested on your Mac first): Pull ~40 fields from each return into rows. Uses: ETL scripts, Postgres staging.
3. **Embed** (OpenAI or Voyage servers, not ours): Turn each mission into 512 numbers. The ETL sends filings, the apps send questions. Uses: Embedding API.
4. **Store** (Supabase): One shared database: tables and vectors together. Every app reads it. Uses: Postgres + pgvector, Auth, Daily backups.
5. **Serve** (Vercel): Apps and API answer searches. Uses: NomBot, GrantRadar, Public API /api/v1/search, Claude API.
6. **Use**: People and partner apps. Uses: Donors, nonprofits, developers, Philanthro Map.

- **APIs:** only `/api/v1/search` is public (rate-limited). The database is never public.
- **Stay small:** vectors for the newest filing per org only. `halfvec` halves their size.
- **Cost:** Supabase with 4 to 8 GB of memory, ~$60 to $110 a month. ETL on GitHub Actions, free.

#### How big it gets

|   | Today | Worst case |
|---|---|---|
| Vectors | 786K | ~2M (every org) |
| Vector index | 1.4 GB | ~3 GB (`halfvec`) |
| Database | 14 GB | ~30 GB (10 IRS years) |

#### Where the embedding model runs

| Option | Cost | Catch |
|---|---|---|
| Embedding API (OpenAI, Voyage) ✓ | Pennies a month | Re-embed filings once, ~$3 |
| Host nomic (Modal, Hugging Face) | ~$5 to $50 a month | One more service to run |
| nomic inside the Vercel app | Free | 550 MB model, slow first searches |
| Supabase built-in (gte-small) | Free | Weaker, 384 dims, schema change |
| Your Mac | Free | Must stay on and online 24/7 |

#### Steps

1. Pick the embedding model (eval: nomic, OpenAI 3-small, Voyage 3.5-lite)
2. Newest filing only, `halfvec`
3. Supabase up, load from local
4. Point the apps at Supabase
5. Monthly ETL on GitHub Actions

</details>

<details><summary><b>Insights</b>: finding hidden patterns, what's missing, build order, tools</summary>

### Finding hidden patterns

| Ready | What we have |
|---|---|
| Meaning | Mission vectors find similar orgs in different words |
| Money | 5.35M grants, funder to grantee, 4 IRS years |
| Place | Orgs and grants on counties, joined to Census need |
| People | 4.86M board seats at 679K orgs. Interlocks built (see below) |
| Time | 225K orgs with 2+ years of filings |

### What's missing

| Gap | Why it blocks discovery | Fix |
|---|---|---|
| Weak links | 44% of grants unlinked. New sources use other IDs (UEI) or only names | ID crosswalk: EIN, UEI, name, address |
| Hidden relationships | Who's connected to whom is buried in JSON and grant rows | Connections table: funds, shares a board member, related org, co-funded |
| One kind of vector | Can't find funders by what they actually fund | Embed grant purposes and funder profiles |
| Thin history | Trends need many years. Search keeps the newest only | Every year as a time series. Add IRS 2021 to 2022 |
| No analytics layer | The model can't read 14 GB. App queries are tuned for lookups | Nightly signal tables. Read-only SQL tool for the model |
| Trust per fact | Name-matched links can be wrong | Source and confidence on every link |

### Patterns it could surface

- **Board interlocks:** the same people on a funder's board and a grantee's.
- **Funding deserts:** high-need counties with few grants and few active nonprofits.
- **Co-funding clusters:** foundations that fund the same groups, so likely next funders for a peer.
- **Early warnings:** falling revenue, a lost federal award and news of layoffs, together.
- **Rising orgs:** fast growth, new funders and new programs in the same year.

### Build order

1. **Connections table** (Board interlocks done, Oct 14): Board interlocks from officer names: 977K same-state pairs, 45K high-confidence hidden links, 13,164 foundations paying an org they share board members with. [Graph view](https://claude.ai/artifact/8u9Nfum4wGRhAZhHJZF2VA). Co-funding next.
2. **ID crosswalk**: Before USAspending and other new sources, so they link on day one.
3. **Grant-purpose vectors** (Embedding API): Search funders by what they fund, not only by who they funded.
4. **Signal tables** (Nightly): Deserts, clusters, trends, warnings, precomputed.
5. **Model tools**: Read-only SQL over the signal tables, so the model asks its own questions.
6. **LLM wiki** (Model-written, on top of the database): Linked pages for hubs (top ~2K funders, causes, states, notable counties), each pattern found, and the Giving Guides. Each page cites its query or source. Research Buddy reads the wiki first, SQL for exact numbers.

### Tools that help

| Tool | Use |
|---|---|
| **Link the same org across sources** | |
| [Splink](https://moj-analytical-services.github.io/splink/) ★ | Matches on name, address and city together, with a confidence score. Millions of rows on a laptop |
| [Claude Batch API](https://docs.anthropic.com/en/docs/build-with-claude/batch-processing) | Settles hard leftover matches by reading both records, at half price |
| **Find connections** | |
| [igraph / NetworkX](https://igraph.org) ★ | Clusters of funders, hidden hubs, likely next funders. Nightly, results back to Postgres |
| [Neo4j Graph Data Science](https://neo4j.com/docs/graph-data-science/current/) | Same algorithms at larger scale, as a separate service |
| [Apache AGE](https://age.apache.org) | Graph queries inside Postgres. Fewer parts, fewer algorithms |
| [Microsoft GraphRAG](https://github.com/microsoft/graphrag) | An entity graph built from text: guides, websites, news |
| **Find themes the IRS codes miss** | |
| [BERTopic](https://maartengr.github.io/BERTopic/) ★ | Natural cause clusters from our vectors, and how they change over time |
| [scikit-learn](https://scikit-learn.org) | Unusual jumps or drops, for early warnings and rising orgs |
| **Let the model explore safely** | |
| [Postgres / Supabase MCP, read-only](https://supabase.com/docs/guides/getting-started/mcp) ★ | Claude runs its own SQL and can't change data |
| [dbt](https://docs.getdbt.com) ★ | Signal tables as tested, documented SQL, so the model reads clear definitions |
| [DuckDB](https://duckdb.org) | Fast heavy analysis on a copy of the data, free, on the Mac |
| **Keep it trustworthy** | |
| [dbt tests / Great Expectations](https://greatexpectations.io) | Catch bad loads: missing years, sudden zeros, duplicate links |
| [Dagster](https://dagster.io) | Pipelines with lineage and freshness checks, if GitHub Actions gets too small |
| **See the patterns** | |
| [Cosmograph](https://cosmograph.app) | Millions of graph links, interactive in a browser |
| [Kepler.gl](https://kepler.gl) | Large maps, such as grants flowing between counties |

★ Top 5, in order: Splink, graph algorithms, dbt, read-only MCP, BERTopic.

</details>

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
