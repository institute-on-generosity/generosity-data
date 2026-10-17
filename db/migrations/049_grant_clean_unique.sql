-- grant_clean (migration 033) gets a unique index on id, so it can be refreshed CONCURRENTLY after grants are
-- relinked (etl/rematch_grants.py) without blocking NomBot's research answers. Additive.
-- Run outside a transaction: psql -d nombot -f db/migrations/049_grant_clean_unique.sql
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS grant_clean_id_uq ON grant_clean (id);
