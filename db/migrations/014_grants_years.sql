-- Several years of 990-PFs from every US foundation: each grant records the return it came from, so a
-- grant cites its own year's filing (funders.object_id is only the newest return).
ALTER TABLE grants ADD COLUMN object_id text;
CREATE INDEX grants_year_idx ON grants (funder_ein, tax_year);
CREATE INDEX grants_state_idx ON grants (recipient_state);
