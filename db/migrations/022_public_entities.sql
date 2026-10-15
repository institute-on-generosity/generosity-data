-- Grant recipients that are not IRS exempt orgs: school districts (NCES CCD) and colleges (IPEDS), so grants to
-- "Altoona Area School District" or a state university can link somewhere. Colleges carry their EIN, which also
-- links private colleges to orgs.
CREATE TABLE IF NOT EXISTS public_entities (
  id     text PRIMARY KEY,          -- 'nces:<LEAID>' or 'ipeds:<UNITID>'
  kind   text NOT NULL,             -- school_district, college_public, college_private
  name   text NOT NULL,
  alias  text,                      -- IPEDS IALIAS (other names, ' | ' separated)
  city   text,
  state  char(2),
  zip    text,
  ein    char(9),                   -- IPEDS EIN when present
  website text
);
CREATE INDEX IF NOT EXISTS public_entities_name ON public_entities (state, org_norm(name));
ALTER TABLE grants ADD COLUMN IF NOT EXISTS recipient_entity text;   -- public_entities.id when the recipient is not an IRS org
