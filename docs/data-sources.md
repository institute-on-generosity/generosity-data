# Data sources

What the shared database (generosity-data) loads now and what is coming, grouped by what each source brings.
Also in the brief's **Data sources** tab ([`shared-database-brief.html`](shared-database-brief.html)). Status: **Connected** = loaded, **Next** = planned soon, **Later** = planned.

## Who exists
*Is it a real, active charity, and can gifts be deducted?*

| Source | Brings | Access | Status |
|---|---|---|---|
| [IRS master file](https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf) | Name, EIN, address, charity type, ruling year | Bulk file | **Connected** |
| [IRS Pub 78](https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-bulk-data-downloads) | Eligible for tax-deductible gifts | Bulk file | **Next** |
| [IRS revocations](https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-bulk-data-downloads) | Lost tax-exempt status, and when | Bulk file | **Next** |
| [SAM.gov Entity API](https://open.gsa.gov/api/entity-api/) | Registered to receive federal money | API key | Later |
| [State charity registries](https://oag.ca.gov/charities) | Registered, delinquent or suspended (CA, NY first) | Bulk file | Later |

## What they do
*Mission, programs and cause, for search*

| Source | Brings | Access | Status |
|---|---|---|---|
| [IRS 990 XML: Parts I, III](https://www.irs.gov/charities-non-profits/form-990-series-downloads) | Mission and program text (embedded for search) | Bulk file | **Connected** |
| [NCCS NTEE codes](https://urbaninstitute.github.io/nccs-legacy/ntee/ntee.html) | Cause category | Lookup | **Connected** |
| Claimed profiles | Programs, needs, contacts, from the nonprofit | Ours | **Next** |
| Org websites | New programs, events, closures | Weekly fetch | Later |

## Financial health
*Can it sustain its work?*

| Source | Brings | Access | Status |
|---|---|---|---|
| [IRS 990 XML: Parts I, VIII to X](https://www.irs.gov/charities-non-profits/form-990-series-downloads) | Revenue, spending, cash, debts, 2 years | Bulk file | **Connected** |
| [IRS SOI extract](https://www.irs.gov/statistics/soi-tax-stats-annual-extract-of-tax-exempt-organization-financial-data) | Revenue, expenses, assets for orgs without e-filed XML | Bulk file | **Connected** |
| [ProPublica API](https://projects.propublica.org/nonprofits/api) | Per-org filing history, fetched live | API | **Connected** |

## Governance and trust
*Is it well run?*

| Source | Brings | Access | Status |
|---|---|---|---|
| [IRS 990 XML: Parts IV, VI, VII](https://www.irs.gov/charities-non-profits/form-990-series-downloads) | Board, policies, audit, insider deals, officer pay | Bulk file | **Connected** |
| [Charity Navigator API](https://developer.charitynavigator.org) | Ratings and advisories | API key | Later |

## Private funding
*Who funds whom*

| Source | Brings | Access | Status |
|---|---|---|---|
| [IRS 990-PF XML: Part XV](https://www.irs.gov/charities-non-profits/form-990-series-downloads) | Every foundation grant, how to apply, deadlines | Bulk file | **Connected** |
| [IRS 990 Schedule I](https://www.irs.gov/charities-non-profits/form-990-series-downloads) | Grants by public charities: DAF sponsors, community foundations, United Ways (3.17M grants, 2021-2025; Schedule D DAF totals) | Bulk file | **Connected** |
| [Philanthropy News Digest](https://philanthropynewsdigest.org) | New grants and programs announced | RSS | Later |

## Public funding
*Government money, now*

| Source | Brings | Access | Status |
|---|---|---|---|
| [IRS 990 XML: Part VIII 1e](https://www.irs.gov/charities-non-profits/form-990-series-downloads) | Government grants received | Bulk file | **Connected** |
| [USAspending award data archive](https://files.usaspending.gov/award_data_archive/) | Federal grants and cooperative agreements to nonprofits, FY2022-2025, by agency, program and place of performance (408,462 award-years) | Bulk file | **Connected** |
| [Grants.gov API](https://www.grants.gov/api) | Open federal grants to apply for | API | **Next** |

## Policy and politics
*What government is about to change*

| Source | Brings | Access | Status |
|---|---|---|---|
| [Congress.gov API](https://api.congress.gov) | Every bill in the current Congress (19,611), refreshed by last update | API key | **Connected** |
| [Federal Register API](https://www.federalregister.gov/developers/documentation/api/v1) | New federal rules and grant notices, daily (24,945 from the past year, 78 funding notices) | API | **Connected** |
| [County election results](https://electionlab.mit.edu/data) | Vote share by county, for issue donors in a midterm year | Bulk file | Later |

## Community need
*Where help is needed*

| Source | Brings | Access | Status |
|---|---|---|---|
| [Census SAIPE + population](https://www.census.gov/data/developers.html) | County poverty, income, population | Bulk file | **Connected** |
| [Census ACS 5-year](https://www.census.gov/programs-surveys/acs/data/summary-file.html) | County poverty, income, age, race and ethnicity, 2020-2024 | Bulk file | **Connected** |
| [OpenFEMA API](https://www.fema.gov/about/openfema/api) | Disaster declarations by county, real-time | API | **Next** |
| [BLS API](https://www.bls.gov/developers/) | County unemployment, monthly | API key | Later |
| [Eviction Lab](https://evictionlab.org) | Eviction filings by place | Bulk file | Later |
| [Map the Meal Gap](https://map.feedingamerica.org) | Food insecurity by county | Bulk file | Later |

## Place
*Put every org and grant on the map*

| Source | Brings | Access | Status |
|---|---|---|---|
| [Census ZIP → county](https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/) | ZIP to county | Bulk file | **Connected** |
| [ARC counties](https://en.wikipedia.org/wiki/List_of_Appalachian_Regional_Commission_counties) | Appalachia flag | Lookup | **Connected** |
| [Census Geocoder API](https://geocoding.geo.census.gov/geocoder/) | Exact address to tract, for finer maps | API | Later |

## Pooled and online giving
*Giving outside foundations*

| Source | Brings | Access | Status |
|---|---|---|---|
| IRS master file + SOI | 145 giving circles that file their own 990 | Bulk file | **Connected** |
| [Philanthropy Together](https://philanthropytogether.org) | Hosted giving circles (not on any 990) | Partner | Later |
| [GlobalGiving API](https://www.globalgiving.org/api/) | Live projects and campaigns | API key | Later |
| [Every.org API](https://docs.every.org) | Nonprofit profiles and fundraisers | API key | Later |

## News and signals
*What changed this week*

| Source | Brings | Access | Status |
|---|---|---|---|
| [GDELT](https://www.gdeltproject.org) | News on philanthropy and nonprofits, 6 topics, US English | API | **Connected** |

## Expert knowledge
*Guidance to cite in answers*

| Source | Brings | Access | Status |
|---|---|---|---|
| IoG GG2026 Giving Guides | 13 guides of IoG research, cited by Research Buddy | Ours | **Next** |
| [RCT and impact-evaluation papers](https://openalex.org) | Evidence on what works, by cause (OpenAlex, J-PAL, 3ie) | API | Later |
| [Impact-investing fund reports](https://thegiin.org) | Reported outcomes from impact funds | Partner | Later |

## From our users
*What people need and do*

| Source | Brings | Access | Status |
|---|---|---|---|
| Feedback, saved lists | Thumbs up or down per result, searches with no good answer | Ours | **Connected** |

## Keeping it fresh

| How | Sources | When |
|---|---|---|
| **Nightly check** | IRS 990s, master file, Pub 78, revocations, Census | Every night, 2 AM |
| **Frequent poll** | FEMA hourly. USAspending, Grants.gov daily | Asks only for records changed since last time |
| **Instant write** | Claimed profiles, feedback, saved lists | The app writes straight to Supabase |
| **Fetch on open** | ProPublica org detail | When a page opens, cached 1 day |

Nightly: check each source's file date, load only new records, embed and link them, publish in one transaction, log rows added per source.
