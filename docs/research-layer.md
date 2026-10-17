# Research layer: data dictionary

Pre-aggregated foundation-grant facts and lookups behind NomBot's research answers (`web/lib/answer.ts`).
Migrations **038** (bands, profiles), **039** (cause taxonomy), **040** (cubes, refresh function).
All of it is built from `grant_clean` (migration 033), so it holds the **same grants with the same exclusions**: sums match `grant_clean` exactly (NomBot `web/eval/answer-cube.mts` answers 51 questions both ways and compares).

**Refresh:** `SELECT refresh_research_layer();` (last step of `db/refresh.sql`, ~40 s, `CONCURRENTLY`, so readers keep the old rows). Run it after `grant_clean`, `flag_latest`, `org_labels` or `county_need` change.

## Read this first: caveats that apply everywhere

| Caveat | What it means |
|---|---|
| **Counted where headquartered** | A grant is placed at the recipient address on the 990-PF, usually a head office. National groups' offices pull dollars to **DC** (few residents, many headquarters), New York and other hubs. DC is shown and flagged as an outlier, never dropped. |
| **Private foundations only** | 990-PF grants. Community foundations, donor-advised funds and corporate giving programs file other forms. |
| **Identified recipients** | About two thirds of grants link to an IRS-registered org (EIN). Cause, NTEE and recipient-size figures cover identified recipients only, so they run low. |
| **Tax years** | 2022 to 2025. 2025 returns are still arriving. |
| **Exclusions** | Amount above 0. No grants to individuals, placeholder lines, foreign recipients or other private foundations (same as `grant_clean`). |
| **NTEE** | The recipient's main code as the IRS assigned it. About a third of orgs have none. Multi-purpose orgs are coded once. |

## Size bands (`size_bands`, functions `revenue_band()`, `asset_band()`)

Fixed edges, so a band always means the same dollars. Answers round a requested cutoff ("under $2M") to the nearest edge and say so.

| kind | band | range |
|---|---|---|
| recipient_revenue | 1 to 8 | under $100K · $100K–500K · $500K–1M · $1M–5M · $5M–10M · $10M–50M · $50M–100M · $100M and up |
| funder_assets | 1 to 6 | under $1M · $1M–10M · $10M–50M · $50M–100M · $100M–1B · $1B and up |

Lower edge inclusive, upper edge exclusive. Negative revenue falls in band 1.

## `recipient_profile` (1,396,774 rows): one row per org with a known revenue

| Column | Meaning | Caveat |
|---|---|---|
| `ein` | Organization EIN (unique) | |
| `revenue` | Annual revenue | Newest e-filed 990 / 990-EZ (`flag_latest`), else the IRS master file amount. Not the grant year. |
| `revenue_source` | `990` or `bmf` | |
| `revenue_year` | Tax year of the 990 used | NULL when from the master file |
| `band` | `revenue_band(revenue)` | Orgs with no revenue anywhere (mostly 990-N filers, usually under $50K) are absent: band unknown |

## `funder_profile` (138,734 rows): one row per 990-PF filer

| Column | Meaning | Caveat |
|---|---|---|
| `ein` | Foundation EIN (unique) | |
| `assets` | Fair market value of assets at year end | Newest 990-PF, not the grant year |
| `asset_band` | `asset_band(assets)` | |
| `funder_type` | `family_named` (23,592) · `operating` (7,792) · `trust` (5,443) · `other` (101,907) | The IRS flags neither family nor company foundations. **family_named** = "Family" in the name, a floor (most family foundations carry a person's name). **operating** = master-file foundation code 02/03. **trust** = 4947(a)(1) trust (subsection 92). First match wins in that order. Company foundations can't be identified (NTEE T21 is unreliable: it codes the Ford Foundation as corporate). |

## `ntee_codes` (681 rows) and `cause_terms` (287 rows), view `cause_lookup` (890 rows)

| Object | Columns | Meaning |
|---|---|---|
| `ntee_codes` | `code`, `name`, `major` (first letter), `draft` | Every NCCS NTEE code: 26 major groups and their detail codes (`etl/ntee_codes.csv`). `draft` = NCCS marks it "not for internal IRS use". |
| `cause_terms` | `term`, `ntee` (prefixes), `label` | Curated plain-English cause names ("animal shelters" → D20, "homelessness" → L41 + P85, "climate" → label `climate`). A prefix matches the start of an org's NTEE code. |
| `cause_lookup` | `term`, `ntee`, `label`, `source` | Curated terms first, then every NTEE code's own name in lowercase. NomBot resolves a cause the router left without a code or label here, before falling back to keyword search. |

## `grant_cube` (948,897 rows): the fact cube

One row per combination of the dimensions below. **Additive measures** can be summed over any rows. **Distinct counts are per row only.**

| Column | Kind | Meaning | Caveat |
|---|---|---|---|
| `tax_year` | dim | Foundation's tax year | |
| `recipient_state` | dim | State of the recipient address on the grant | Headquarters effect (DC) |
| `rural` | dim | true = the recipient ZIP's county has under 50,000 people (`county_need`) · false = 50,000+ · NULL = ZIP not mapped | A population proxy, not the USDA rural-urban code |
| `ntee3` | dim | First 3 characters of the recipient's NTEE code | NULL for unidentified or uncoded recipients. Prefixes up to 3 characters match exactly as on the full code |
| `recipient_kind` | dim | `nonprofit` (identified EIN) · `school_district` · `college_public` · `college_private` (from `public_entities`) · `unidentified` | School districts and colleges are matched by name and address to NCES / IPEDS lists. A grant naming only a school or department can be missed |
| `linked` | dim | Recipient has an EIN | Some school districts and colleges also have one |
| `recipient_band` | dim | `recipient_profile.band` | NULL = unidentified or revenue unknown |
| `funder_band` | dim | `funder_profile.asset_band` | |
| `funder_type` | dim | `funder_profile.funder_type` | See funder_profile |
| `labels` | dim | Sorted `label:tier` array from `org_labels`, e.g. `{climate:primary}` | `{}` = no label. Labels overlap each other and NTEE groups |
| `usd` | additive | Grant dollars | |
| `n` | additive | Grant lines | One 990-PF line each. Installments count separately |
| `gos_usd`, `gos_n` | additive | Dollars and lines whose purpose reads general / unrestricted / operating | A text rule, a floor |
| `recipients`, `funders` | per row | Distinct recipient EINs and funder EINs in this row | **Not additive.** For exact counts across rows use `grant_recipient_year` or `grant_clean` |

## `grant_recipient_year` (765,713 rows): identified recipients by year

One row per recipient EIN, tax year, recipient state and county (a recipient's grants can carry different addresses). Columns: `recipient_ein`, `tax_year`, `recipient_state`, `county_fips`, `rural`, `recipient_ntee` (full code), `recipient_band`, `labels`, `usd`, `n`, `gos_usd`, `funders` (distinct within the row, not additive). Used for exact distinct-recipient counts and concentration (top 1% of recipients). No funder columns: questions filtered by funder size or type use `grant_clean`.

## `grant_county_year` (134,555 rows): county rollup

`tax_year`, `county_fips` (NULL = ZIP not mapped), `recipient_state`, `ntee_major` (first letter, NULL = uncoded or unidentified), `linked`, `usd`, `n`. All clean grants, for county rankings and county per-resident figures (population from `county_need`, Census 2024).

## What uses what (NomBot `lib/answer.ts`)

| Question | Source |
|---|---|
| Totals, shares, trends, compare (label or NTEE cause up to 3 characters, no county) | `grant_cube` sums + exact distinct counts (`grant_recipient_year` recipients, `grant_clean` funders) |
| By state, rural vs urban, fastest-growing causes, general support share | `grant_cube` |
| Breakdowns by cause, NTEE code, recipient size, foundation size or type, recipient kind, year | `grant_cube` |
| Breakdown by county | `grant_county_year` (all causes or NTEE major groups), else `grant_clean` |
| Concentration | `grant_recipient_year` |
| Keyword causes, a named county, medians, top funders / recipients, out-of-state, overlap | `grant_clean` (+ profiles for filters) |
