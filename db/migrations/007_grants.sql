-- GrantRadar: private foundations (990-PF filers) and the grants they paid (Part XV line 3a),
-- loaded by etl/load_grants.py. Recipients are listed by name + address, rarely by EIN, so the
-- loader links each grant to `orgs` with a match score; weak links stay NULL.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE funders (
  ein                char(9) PRIMARY KEY,
  object_id          text NOT NULL,          -- IRS e-file id of the 990-PF the row came from
  tax_year           int,
  name               text NOT NULL,
  city               text,
  state              char(2),
  assets             bigint,                 -- fair market value of assets, end of year
  grants_paid        bigint,                 -- total grants paid during the year
  grant_count        int NOT NULL DEFAULT 0,
  invite_only        boolean NOT NULL,       -- "only makes contributions to preselected charitable organizations"
  website            text,
  apply_contact      text,                   -- Part XV 2a-d: how to apply, when present
  apply_form         text,
  apply_deadlines    text,
  apply_restrictions text
);

CREATE TABLE grants (
  id               bigserial PRIMARY KEY,
  funder_ein       char(9) NOT NULL REFERENCES funders ON DELETE CASCADE,
  tax_year         int,
  recipient_name   text NOT NULL,
  recipient_city   text,
  recipient_state  char(2),
  recipient_zip    text,
  amount           bigint,
  purpose          text,
  recipient_status text,                     -- e.g. PC (public charity), GOV, PF
  relationship     text,
  recipient_ein    char(9),                  -- matched organization in `orgs`, or NULL
  match_score      real,                     -- 1 = exact name in the same state; trigram similarity otherwise
  match_method     text                      -- exact | exact-city | fuzzy
);
CREATE INDEX grants_funder_idx ON grants (funder_ein);
CREATE INDEX grants_recipient_idx ON grants (recipient_ein) WHERE recipient_ein IS NOT NULL;

-- Normalized organization name: what's left after case, punctuation and legal suffixes.
CREATE FUNCTION org_norm(n text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT btrim(regexp_replace(
    regexp_replace(' ' || regexp_replace(upper(replace(coalesce(n, ''), '&', ' AND ')), '[^A-Z0-9]+', ' ', 'g') || ' ',
                   ' (THE|INC|INCORPORATED|CORP|CORPORATION|CO|LLC|LTD|OF|AND) ', ' ', 'g'),
    '\s+', ' ', 'g'))
$$;
CREATE INDEX orgs_norm_idx ON orgs (state, org_norm(name));
CREATE INDEX orgs_norm_trgm_idx ON orgs USING gin (org_norm(name) gin_trgm_ops);
