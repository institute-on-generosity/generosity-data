-- Rebuild the newest-record views (migration 015) after loading or embedding.
-- Plain (not CONCURRENTLY) refresh: it rebuilds the HNSW index in one bulk pass, which with enough memory
-- takes minutes; CONCURRENTLY updates it row by row and ran 40+ minutes after a full re-embed without
-- finishing. Readers wait while each view rebuilds. The default 64MB maintenance_work_mem is far too small
-- for a ~2GB HNSW graph.
SET maintenance_work_mem = '4GB';
SET max_parallel_maintenance_workers = 7;
\timing on
REFRESH MATERIALIZED VIEW filing_latest;
REFRESH MATERIALIZED VIEW financials_latest;
REFRESH MATERIALIZED VIEW flag_latest;
REFRESH MATERIALIZED VIEW funder_purposes;
-- After grants are relinked: board links marked funder-grantee need grants.recipient_ein (migration 018)
REFRESH MATERIALIZED VIEW board_interlocks;
-- Research answers (migration 033): after grants are relinked or 990 filings load
REFRESH MATERIALIZED VIEW grant_clean;
REFRESH MATERIALIZED VIEW grant_totals;
REFRESH MATERIALIZED VIEW flag_year;
-- Research layer (migrations 038-040): profiles, grant_cube, grant_recipient_year, grant_county_year; and
-- Schedule I (042): charity_funder_profile, charity_grant_clean, charity_grant_cube (after etl/load_charity_grants.py).
-- Concurrent refreshes (readers keep the old rows), ~1 minute. Also run after org_labels or county_need change.
-- county_acs (043) and federal_awards (044) are plain tables: their loaders write them directly.
SET work_mem = '512MB';
SELECT refresh_research_layer();
