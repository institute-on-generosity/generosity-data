# Database health

*Generated 2026-10-15 15:20 PDT by `etl/db_health.py`. Database: 15 GB.*

## Checks

- ✅ Every filing embedded with the current model (local:nomic-embed-text-v1.5-fp32@512)
- ✅ Search index view populated (541,879 orgs)
- ✅ Board interlocks built
- ⚠️ Grants linked to a recipient: 63% of all, 65% of those that can link (goal 70% of all)

## What's in it

| | Count |
|---|---|
| Organizations (IRS master file) | 1,972,852 |
| Orgs with an e-filed 990 (searchable) | 541,879 |
| 990 filings (text + diligence fields) | 785,772, 785,772 with vectors |
| Foundations (990-PF) | 138,734 |
| Grants | 5,348,919, $392B, tax years 2019–2025 |
| Grants linked to a recipient (org, school district or college) | 3,390,769 (63%; 65% of the 5,250,947 not foreign, individual or placeholder) |
| Board seats | 4,859,408 at 679,042 orgs |
| Board interlocks (same-state org pairs) | 977,088: 46,045 high-confidence hidden links, 14,569 foundation-grantee |
| Giving circles | 145 |
| Counties with need data | 3,144 |

## Freshness

- Newest 990 loaded: object id `202642399349301964` (IRS batch year 2026)
- Filings by tax year: 2025: 162,042, 2024: 490,916, 2023: 126,047, 2022: 6,718, 2021: 49
- Latest migrations: `023_recipient_kind.sql` (2026-10-15), `022_public_entities.sql` (2026-10-15), `021_grant_unlinked.sql` (2026-10-15), `020_match_aliases.sql` (2026-10-14), `019_news_policy.sql` (2026-10-14)

Latest run per live source (`source_updates`):

| Source | Last run | Rows added | Newest item | Note |
|---|---|---|---|---|
| congress | 2026-10-14 19:39 | 0 | 2026-10-15 | congress 119, fetched 19649 of 19649 |
| federal_register | 2026-10-14 19:06 | 24,945 | 2026-10-14 | fetched 24945 since 2025-10-14 |

## Tables

| Table | Rows (approx.) | Size | Holds |
|---|---|---|---|
| `filing_latest` (view) | 542,768 | 3593 MB | View: newest filing per org (search index) |
| `filing_text` | 785,789 | 3510 MB | Mission and programs per 990, with vectors |
| `grants` | 5,341,226 | 1979 MB | Every 990-PF grant, linked to recipients by name |
| `board_members` | 4,859,408 | 1473 MB | Board seats from 990 Part VII and 990-PF officers |
| `orgs` | 1,972,446 | 1138 MB | Every exempt organization (IRS master file) |
| `financials` | 538,959 | 928 MB | Revenue, expenses, assets by year (IRS SOI) |
| `filing_flag` | 785,862 | 894 MB | Diligence fields per 990: finances, board, policies, officers |
| `rm_o` | 1,972,852 | 591 MB |  |
| `rm_addr` | 2,280,039 | 211 MB |  |
| `board_interlocks` (view) | 977,088 | 192 MB | View: org pairs that share board members |
| `rm_ostreet` | 1,569,622 | 153 MB |  |
| `rm_u` | 931,643 | 147 MB |  |
| `rm_top` | 20,000 | 117 MB |  |
| `funder_purposes` (view) | 983,503 | 110 MB | View: grant purposes per funder |
| `flag_latest` (view) | 541,879 | 58 MB | View: newest diligence fields per org |
| `financials_latest` (view) | 498,656 | 56 MB | View: newest financials per org |
| `rm_cand` | 516,169 | 52 MB |  |
| `federal_register_docs` | 24,945 | 37 MB | Federal Register rules, notices, funding notices |
| `funders` | 138,734 | 34 MB | Private foundations, newest 990-PF |
| `congress_bills` | 19,611 | 27 MB | Bills in the current Congress (Congress.gov) |
| `grant_unlinked` | 163,330 | 25 MB | Links removed after grading, with the reason |
| `public_entities` | 25,511 | 6488 kB | School districts (NCES) and colleges (IPEDS) for grants to non-IRS recipients |
| `rm_fz2c` | 84,191 | 4656 kB |  |
| `rm_fz2` | 32,116 | 4656 kB |  |
| `rm_fz2r` | 32,116 | 4480 kB |  |
| `org_dba` | 28,632 | 3824 kB | Doing-business-as names from 990 headers |
| `rm_top2` | 20,000 | 3648 kB |  |
| `grant_match_overrides` | 17,236 | 3640 kB | Hand and Claude-confirmed recipient links |
| `rm_topc` | 58,121 | 3408 kB |  |
| `rm_top2c` | 54,336 | 3120 kB |  |
| `zip_regions` | 34,236 | 3048 kB | ZIP to county, Appalachia flag |
| `rm_fz` | 20,000 | 2896 kB |  |
| `rm_fzc` | 54,531 | 2832 kB |  |
| `rm_fzr` | 20,000 | 2824 kB |  |
| `rm_pick2` | 20,000 | 2040 kB |  |
| `rm_pick` | 20,000 | 2024 kB |  |
| `county_need` | 3,145 | 872 kB | County poverty, income, population (Census) |
| `rm_nat` | 4,931 | 560 kB |  |
| `rm_pe` | 4,131 | 504 kB |  |
| `soi_fields` | 499 | 216 kB |  |
| `rm_natok` | 1,146 | 128 kB |  |
| `feedback` | 0 | 64 kB | Thumbs up or down from NomBot users |
| `source_updates` | 0 | 48 kB | Freshness log: one row per loader run |
| `news_articles` | 0 | 40 kB | GDELT news on philanthropy and nonprofits |
| `schema_migrations` | 0 | 32 kB |  |

Row counts for tables come from Postgres statistics (refreshed by ANALYZE), so they can lag a load slightly; the counts above are exact.
