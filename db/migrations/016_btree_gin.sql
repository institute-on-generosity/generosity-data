-- btree_gin: lets etl/load_grants.py index (state, name trigrams) together, so fuzzy recipient
-- matching only searches orgs in the recipient's state (nationally, days -> about an hour).
CREATE EXTENSION IF NOT EXISTS btree_gin;
