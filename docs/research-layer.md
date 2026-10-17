# Research layer: data dictionary

Pre-aggregated foundation-grant facts and lookups behind NomBot's research answers (`web/lib/answer.ts`).
Migrations **038** (bands, profiles), **039** (cause taxonomy), **040** (cubes, refresh function), **041-042** (Schedule I grants by charities), **043** (Census ACS by county), **044** (federal awards to nonprofits).
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

## Schedule I grants by charities (migrations 041-042)

Grants that **public charities** report on Form 990 **Schedule I Part II** (grants over $5,000 to US organizations and governments): donor-advised fund sponsors, community foundations, United Ways, universities, hospitals, federations. Loaded by `etl/load_charity_grants.py` from the same IRS e-file batches as the 990-PF grants (processing years 2023-2026; `YEAR=2023 TYPES=990 etl/fetch_990_xml.sh` extracts a year's Form 990s). Kept apart from `grants`, so foundation answers don't change. Grants to individuals (Part III) are not loaded.

**Coverage (Oct 17, 2026):** 285,635 returns with a Schedule I from 111,461 filers · 3,166,830 grant lines · $602B (tax years 2019-2025; 2022: 882,769 lines $166.3B, 2023: 810,753 $150.3B, 2024: 928,395 $176.8B, 2025 partial: 116,742 $9.1B). **Linked to an org: 83.8% of lines, 84.4% of dollars** (2,562,918 by the reported EIN, then name matcher: 54,546 exact, 1,423 exact-city, 36,202 fuzzy). 94.3% of lines carry a recipient EIN.

**Checked against filers' own totals** (Schedule I sum vs Part IX line 1, grants to domestic organizations): Fidelity Charitable 2024 $15.07B vs $15.23B, Schwab Charitable 2024 $8.62B vs $8.76B, National Philanthropic Trust 2024 $5.99B vs $6.11B, Silicon Valley Community Foundation 2024 $3.16B vs $3.16B (2022: $2.06B vs $3.63B, part of that year's list is missing from the e-file).

| Caveat | What it means |
|---|---|
| **Which grants a donor advised** | Schedule I doesn't say. A DAF sponsor's grants are mostly DAF grants (that's how `daf_sponsor` is defined); a community foundation's mix DAF and discretionary grants. Schedule D gives each filer's DAF grant total, not by recipient. |
| **Missing returns** | Fidelity Charitable's tax year 2023 return (period ending June 2024) is not in the IRS 2025 batches: batch 05A is cut short on the IRS server. 2023 DAF totals run low. |
| **Double counting** | Grants to another DAF sponsor, community foundation or United Way (`to_grantmaker`, 54,065 lines, $34.0B in 2022-25) are left out of answers: that money is counted when it's granted again. |
| **In-kind** | `amount` = cash + non-cash. Food banks (Feeding America) and donated-goods charities (Good360) report large in-kind grants. |
| **Threshold** | Schedule I lists grants over $5,000 per recipient. Smaller grants are only in Part IX totals. |
| **Place** | The recipient address on Schedule I, usually a headquarters (same DC effect as 990-PF grants). |

### `charity_filers` (285,635 rows): one row per Form 990 with a Schedule I

`object_id` (PK), `ein`, `tax_year`, `name`, `city`, `state`, `holds_dafs` (Part IV line 6), `daf_count`, `daf_contributions`, `daf_grants`, `daf_value` (Schedule D Part I, DAF column), `grants_domestic` (Part IX line 1), `sched_i_rows`, `sched_i_usd`. Newest return per filer and tax period.

### `charity_grants` (3,166,830 rows): one row per Schedule I line

| Column | Meaning |
|---|---|
| `funder_ein`, `object_id`, `tax_year` | The filing charity and its return |
| `recipient_name`, `recipient_city`, `recipient_state`, `recipient_zip` | As written. `recipient_country` set only for a foreign address (1,451 lines) |
| `reported_ein` | RecipientEIN as written |
| `recipient_ein`, `match_method`, `match_score` | Link to `orgs`: `ein` (the reported EIN is an org we know), else `exact` / `exact-city` / `fuzzy` from `etl/org_matcher.py` (the 990-PF matcher's rules) |
| `irc_section` | Recipient's section as written (501(c)(3), government ...) |
| `cash`, `noncash`, `amount` | amount = cash + non-cash |
| `purpose`, `noncash_desc` | Free text |

### `charity_funder_profile` (111,461 rows): one row per filer

`ein`, `name`, `state`, `ntee`, `holds_dafs`, `daf_count`, `daf_value`, `daf_share` (Schedule D DAF grants / Schedule I grants, years with both), `sched_i_usd`, **`funder_class`** (first match wins):

| funder_class | Rule | Filers · Schedule I $ (all years) |
|---|---|---|
| `united_way` | "United Way" in the name | 856 · $5.7B |
| `community_foundation` | "Community Foundation / Fund / Trust" in the name | 1,778 · $44.2B (with the T31 rule below) |
| `daf_sponsor` | Holds DAFs and DAF grants are at least half of its Schedule I grants (Fidelity, Schwab, NPT, Vanguard, Goldman Sachs, National Christian, Jewish Communal Fund, American Endowment Foundation ...) | 469 · $160.5B |
| `community_foundation` | Otherwise NTEE T31 | |
| `other` | Every other charity reporting grants | 108,358 · $391.8B |

### `charity_grant_clean` (2,673,218 rows) and `charity_grant_cube` (292,748 rows)

`charity_grant_clean`: tax years 2022-2025, amount above 0, US state, no foreign address, no private-foundation recipients (as `grant_clean`), plus `funder_class`, `funder_state`, `county_fips`, `recipient_ntee`, `gos` (purpose text reads general / operating) and `to_grantmaker`. **Same column names as `grant_clean`**, so the answer engine reads either. `charity_grant_cube`: sums by `tax_year`, `recipient_state`, `rural`, `ntee3`, `linked`, `funder_class`, `to_grantmaker`, `labels`; additive `usd`, `n`, `gos_usd`, `gos_n`. NomBot source `charity_grants` reads the cube for totals, states, causes and funder groups, and `charity_grant_clean` for counties, keyword causes, top funders and recipients. Refreshed by `refresh_research_layer()`.

## `county_acs` (3,196 rows): Census ACS 5-year by county (migration 043)

American Community Survey **5-year estimates, 2020-2024** (the newest release), from the Census table-based summary files (`etl/load_acs.py`; the Census API now asks for a key). **3,144 counties** (every county in `county_need`), **51 state rows** (`county_fips` = state FIPS + `000`) and the nation (`00000`). Puerto Rico is left out, as in `county_need`. Joins to `county_need` on `county_fips`. `county_need` (SAIPE 2023 poverty, 2024 population estimates) is unchanged and still drives per-resident figures.

| Column | Meaning | Table |
|---|---|---|
| `level`, `name`, `state`, `acs_year` | county / state / us; name and postal code from `county_need`; 2024 = 2020-2024 | |
| `population` | Total population | B01003 |
| `poverty_universe`, `people_in_poverty`, `poverty_moe`, `poverty_rate` | People whose poverty status is known, those below the line, its 90% margin, rate in percent | B17001 |
| `median_income` | Median household income, 2024 dollars | B19013 |
| `under18`, `age65_plus` | Residents under 18, 65 and over | B01001 |
| `white_nh`, `black_nh`, `aian_nh`, `asian_nh`, `nhpi_nh`, `other_nh`, `multi_nh`, `hispanic` | Race alone, not Hispanic or Latino; Hispanic or Latino of any race | B03002 |
| `poc_share` | People of color = 1 - non-Hispanic white / total, percent | B03002 |

**Caveats:** 5-year estimates average 2020-2024, so they lag fast change. Small counties carry wide margins (`poverty_moe`). ACS poverty (40.7M people nationally) and SAIPE (40.8M) differ slightly by county. **Used by NomBot** for `perPoverty` (money per person in poverty, by state or county) and for breakdowns of counties by poverty rate, median income, share of people of color and share aged 65+ (`county_poverty`, `county_income`, `county_race`, `county_age`), placing grants in the recipient's county (headquarters effect applies).

## `federal_awards` (408,462 rows): federal grants to nonprofits (migration 044)

Federal **grants and cooperative agreements** (assistance types 02 block, 03 formula, 04 project, 05 cooperative agreement) to recipients USAspending.gov tags as **nonprofit** (with or without 501(c)(3)) or **private college**, with no government or public-college tag. **Federal fiscal years 2022-2025** (October to September; FY2025 is complete). Loaded by `etl/load_federal_awards.py` from the USAspending award data archive (`FY<yyyy>_All_Assistance_Full_20261006.zip`, ~1.3-1.6 GB each, in `data/gov/usaspending/`, git-ignored). Transactions are summed per award and fiscal year.

**Coverage (Oct 17, 2026):** 408,462 award-years, $272.0B net obligations (FY2022 101,775 · $56.8B, FY2023 102,793 · $64.1B, FY2024 108,991 · $90.3B, FY2025 94,903 · $60.8B). FY2024 includes $20B of EPA Greenhouse Gas Reduction Fund awards to Climate United, Coalition for Green Capital, Power Forward, Opportunity Finance Network, Inclusiv and Justice Climate Fund. 34 awarding agencies, 1,445 assistance listings. **Linked to an org (name + state + city, `etl/org_matcher.py`): 80.6% of award-years, 83.7% of dollars**, 23,557 distinct EINs (exact 278,353, fuzzy 49,704, exact-city 1,147). Spot-check of 14 random links: all right.

| Column | Meaning |
|---|---|
| `award_key`, `fiscal_year` | USAspending award key; one row per award and fiscal year (unique) |
| `fain`, `agency_code`, `agency_name`, `sub_agency_name` | Award id, awarding agency |
| `cfda_number`, `cfda_title` | Assistance listing (program), e.g. 93.600 Head Start |
| `assistance_type` | 02-05 |
| `recipient_uei`, `recipient_name`, `recipient_city`, `recipient_state`, `recipient_zip` | Recipient as registered in SAM |
| `business_types`, `recipient_kind` | USAspending codes; `nonprofit_501c3` (M), `nonprofit_other` (N, e.g. rural electric co-ops), `private_college` (O) |
| `pop_scope`, `pop_state`, `pop_county_fips` | **Place of performance** (where the work happens). 24,621 award-years ($37.5B) have no state (multi-state, foreign); 170,982 ($100.4B) have no county (statewide, multi-state) |
| `amount`, `actions` | Net federal_action_obligation in the fiscal year (deobligations net out, so a row can be negative); number of transactions |
| `description` | Award description |
| `recipient_ein`, `match_method`, `match_score` | Link to `orgs` |

| Caveat | What it means |
|---|---|
| **Not the same as 990 government grants** | Form 990 line 1e counts federal, state and local grants together, including federal money passed through states. USAspending has only direct (prime) federal awards. For Ohio's 393 linked recipients with a 2024 Form 990, FY2024 federal awards were $1.2B against $17.2B reported on line 1e. |
| **Left out** | Contracts, loans, direct payments, subawards, grants to governments and public universities, and USAID's "Miscellaneous Foreign Awardees" aggregate line (1,057 rows). |
| **Fiscal years** | Federal fiscal years, not the tax years of the 990 sources. |
| **No EIN** | USAspending carries a UEI, not an EIN, so links are by name and place. Causes (NTEE, labels) cover linked recipients only. |

NomBot source `federal_awards` reads this table directly (`federal_awards fa LEFT JOIN orgs o`): totals and trends, states and counties by place of performance (per resident or per person in poverty), agencies (`top_funders`), programs, recipients, award sizes and ACS county groups. Totals carry the 990 line 1e comparison for the same linked recipients.

## What uses what (NomBot `lib/answer.ts`)

| Question | Source |
|---|---|
| Totals, shares, trends, compare (label or NTEE cause up to 3 characters, no county) | `grant_cube` sums + exact distinct counts (`grant_recipient_year` recipients, `grant_clean` funders) |
| By state, rural vs urban, fastest-growing causes, general support share | `grant_cube` |
| Breakdowns by cause, NTEE code, recipient size, foundation size or type, recipient kind, year | `grant_cube` |
| Breakdown by county | `grant_county_year` (all causes or NTEE major groups), else `grant_clean` |
| Concentration | `grant_recipient_year` |
| Keyword causes, a named county, medians, top funders / recipients, out-of-state, overlap | `grant_clean` (+ profiles for filters) |
| Schedule I grants (source `charity_grants`, filter `charityFunder`) | `charity_grant_cube`, else `charity_grant_clean` (+ `charity_funder_profile` for funder names) |
| Per person in poverty; counties grouped by poverty, income, race/ethnicity, age | `county_acs` with `grant_county_year`, `grant_clean`, `charity_grant_clean` or `flag_year` by county |
| Federal grants to nonprofits (source `federal_awards`) | `federal_awards` (+ `orgs` for causes, `county_acs` / `county_need` for per-person figures) |
