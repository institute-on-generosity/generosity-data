# Database health

*Generated 2026-10-17 10:03 PDT by `etl/db_health.py`. Database: 21 GB.*

## Checks

- ✅ Every filing embedded with the current model (local:nomic-embed-text-v1.5-fp32@512)
- ✅ Search index view populated (573,199 orgs)
- ✅ Board interlocks built
- ⚠️ Grants linked to a recipient: 64% of all, 65% of those that can link (goal 70% of all)

## What's in it

| | Count |
|---|---|
| Organizations (IRS master file) | 1,972,852 |
| Orgs with an e-filed 990 (searchable) | 573,199 |
| 990 filings (text + diligence fields) | 907,095, 907,095 with vectors |
| Foundations (990-PF) | 139,749 |
| Grants | 5,715,311, $413B, tax years 2019–2025 |
| Grants linked to a recipient (org, school district or college) | 3,666,275 (64%; 65% of the 5,610,054 not foreign, individual or placeholder) |
| Board seats | 5,103,008 at 711,237 orgs |
| Board interlocks (same-state org pairs) | 553,511: 48,586 high-confidence hidden links, 15,505 foundation-grantee |
| Giving circles | 145 |
| Counties with need data | 3,144 |

## Freshness

- Newest 990 loaded: object id `202642399349301964` (IRS batch year 2026)
- Filings by tax year: 2025: 199,027, 2024: 550,228, 2023: 150,581, 2022: 7,210, 2021: 49
- Latest migrations: `046_grant_size_hist.sql` (2026-10-17), `045_company_foundations.sql` (2026-10-17), `044_federal_awards.sql` (2026-10-17), `043_county_acs.sql` (2026-10-17), `042_charity_research.sql` (2026-10-16)

Latest run per live source (`source_updates`):

| Source | Last run | Rows added | Newest item | Note |
|---|---|---|---|---|
| congress | 2026-10-14 19:39 | 0 | 2026-10-15 | congress 119, fetched 19649 of 19649 |
| federal_register | 2026-10-14 19:06 | 24,945 | 2026-10-14 | fetched 24945 since 2025-10-14 |

## Tables

| Table | Rows (approx.) | Size | Holds |
|---|---|---|---|
| `filing_text` | 906,843 | 3922 MB | Mission and programs per 990, with vectors |
| `filing_latest` (view) | 573,387 | 3245 MB | View: newest filing per org (search index) |
| `grants` | 5,715,021 | 2006 MB | Every 990-PF grant, linked to recipients by name |
| `board_members` | 6,502,695 | 1501 MB | Board seats from 990 Part VII and 990-PF officers |
| `charity_grants` | 3,395,801 | 1428 MB |  |
| `orgs` | 1,972,446 | 1138 MB | Every exempt organization (IRS master file) |
| `filing_flag` | 907,364 | 1032 MB | Diligence fields per 990: finances, board, policies, officers |
| `grant_size_hist` (view) | 3,180,928 | 986 MB |  |
| `financials` | 538,959 | 928 MB | Revenue, expenses, assets by year (IRS SOI) |
| `grant_clean` (view) | 5,289,120 | 641 MB |  |
| `rm_o` | 1,972,852 | 591 MB |  |
| `charity_grant_size_hist` (view) | 2,229,445 | 566 MB |  |
| `federal_awards` | 408,462 | 501 MB |  |
| `charity_grant_clean` (view) | 2,673,063 | 463 MB |  |
| `grant_cube` (view) | 995,187 | 306 MB |  |
| `rm_addr` | 2,280,039 | 211 MB |  |
| `grant_recipient_year` (view) | 803,981 | 179 MB |  |
| `flag_year` (view) | 896,197 | 156 MB |  |
| `rm_ostreet` | 1,569,622 | 153 MB |  |
| `rm_u` | 931,643 | 147 MB |  |
| `recipient_profile` (view) | 1,396,774 | 141 MB |  |
| `board_interlocks` (view) | 553,511 | 140 MB | View: org pairs that share board members |
| `rm_top` | 20,000 | 117 MB |  |
| `funder_purposes` (view) | 1,020,363 | 114 MB | View: grant purposes per funder |
| `charity_grant_cube` (view) | 301,963 | 80 MB |  |
| `flag_latest` (view) | 573,199 | 71 MB | View: newest diligence fields per org |
| `charity_filers` | 285,635 | 68 MB |  |
| `pf_filer_address` | 461,677 | 66 MB |  |
| `fed_award_size_hist` (view) | 227,582 | 64 MB |  |
| `financials_latest` (view) | 498,656 | 56 MB | View: newest financials per org |
| `rm_cand` | 516,169 | 52 MB |  |
| `funders` | 139,749 | 38 MB | Private foundations, newest 990-PF |
| `federal_register_docs` | 24,945 | 37 MB | Federal Register rules, notices, funding notices |
| `congress_bills` | 19,611 | 27 MB | Bills in the current Congress (Congress.gov) |
| `org_labels` | 46,846 | 26 MB |  |
| `grant_unlinked` | 163,330 | 25 MB | Links removed after grading, with the reason |
| `grant_county_year` (view) | 139,214 | 21 MB |  |
| `charity_funder_profile` (view) | 113,774 | 16 MB |  |
| `pf_business_contributors` | 107,182 | 16 MB |  |
| `funder_profile` (view) | 139,749 | 15 MB |  |
| `label_candidates` | 92,553 | 12 MB |  |
| `public_entities` | 25,511 | 11 MB | School districts (NCES) and colleges (IPEDS) for grants to non-IRS recipients |
| `org_dba` | 38,533 | 5256 kB | Doing-business-as names from 990 headers |
| `rm_fz2c` | 84,191 | 4656 kB |  |
| `rm_fz2` | 32,116 | 4656 kB |  |
| `rm_fz2r` | 32,116 | 4480 kB |  |
| `grant_match_overrides` | 17,236 | 3800 kB | Hand and Claude-confirmed recipient links |
| `rm_top2` | 20,000 | 3648 kB |  |
| `rm_topc` | 58,121 | 3408 kB |  |
| `rm_top2c` | 54,336 | 3120 kB |  |
| `zip_regions` | 34,236 | 3048 kB | ZIP to county, Appalachia flag |
| `rm_fz` | 20,000 | 2896 kB |  |
| `rm_fzc` | 54,531 | 2832 kB |  |
| `rm_fzr` | 20,000 | 2824 kB |  |
| `rm_pick2` | 20,000 | 2040 kB |  |
| `rm_pick` | 20,000 | 2024 kB |  |
| `insights` | 34 | 960 kB |  |
| `county_need` | 3,145 | 872 kB | County poverty, income, population (Census) |
| `county_acs` | 3,196 | 600 kB |  |
| `rm_nat` | 4,931 | 560 kB |  |
| `rm_pe` | 4,131 | 504 kB |  |
| `soi_fields` | 499 | 216 kB |  |
| `rm_natok` | 1,146 | 128 kB |  |
| `research_questions` | 17 | 120 kB |  |
| `ntee_codes` | 681 | 112 kB |  |
| `cause_terms` | 287 | 88 kB |  |
| `cause_labels` | 0 | 80 kB |  |
| `feedback` | 0 | 64 kB | Thumbs up or down from NomBot users |
| `grant_totals` (view) | 225 | 56 kB |  |
| `api_keys` | 0 | 48 kB |  |
| `source_updates` | 0 | 48 kB | Freshness log: one row per loader run |
| `news_articles` | 0 | 40 kB | GDELT news on philanthropy and nonprofits |
| `schema_migrations` | 0 | 32 kB |  |
| `api_usage` | 0 | 32 kB |  |
| `public_entity_aliases` | 0 | 32 kB |  |
| `size_bands` | 0 | 32 kB |  |
| `company_foundations` | 0 | 16 kB |  |

Row counts for tables come from Postgres statistics (refreshed by ANALYZE), so they can lag a load slightly; the counts above are exact.
