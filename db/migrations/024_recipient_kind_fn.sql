-- recipient_kind as a function, so etl/load_grants.py re-applies it after every load (023 ran once).
-- Adds patterns found Oct 15 among unmatched grants: HIPAA wording, "N recipients/students", "ATCH 4",
-- "vials of X were distributed", "misc ...", "... less than 5000", "details available upon request",
-- "2023 grants", "grants paid", "total". Graded on a fresh sample of flagged names (README).
CREATE OR REPLACE FUNCTION recipient_kind_of(name text, state text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
SELECT CASE
  WHEN coalesce(state, '') !~ '^(A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|PR|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])$' THEN 'foreign'
  WHEN name ~* '(individual|patient|scholarship recipient|eligible|needy|student aid|hip+a|confidential|anonymous)' THEN 'individual'
  WHEN name ~* '(^\s*(see|refer to)\M|attach|statement|schedule|various|^\s*n/?a\M|^grants?$|not listed|list availab)' THEN 'placeholder'
  WHEN name ~* '(health insurance portab|^\s*redacted\s*$|name withheld|^\s*[0-9]* ?unknown (male|female|person)|^\s*(financial|medical|financial (and|&) medical) assistance\M)'
    OR (name ~* '\m[0-9]+ (recipients|individuals|students|families)\M' AND name !~* '\m(of|net|for the) [0-9]+ ') THEN 'individual'
  WHEN name ~* '(^\s*atch\M|\mvials? of\M.*\mdistributed|donations? of (inventor|product)|\m20[0-9]{2} (grants|contributions|donations)\M|^\s*(cash )?(contributions|donations|gifts|grants)\s*$|future payment|see list|^\s*(misc|miscellaneous)\M|less than \$?[0-9]|^\s*-+\s*$|^\s*totals?\s*($|[-:(]))'
    OR (name ~* '(avail|available|availabe|provided|details?|info|information)\M.{0,25}\m(up)?on req' AND name !~* 'address')
    OR (name ~* '(contributions|gifts|grants|donations) paid\M' AND name !~* '\m(union|university|college|foundation|inc|school|hospital|church)\M') THEN 'placeholder'
END $$;
UPDATE grants SET recipient_kind = recipient_kind_of(recipient_name, recipient_state)
WHERE recipient_ein IS NULL AND recipient_entity IS NULL AND recipient_kind IS NULL
  AND recipient_kind_of(recipient_name, recipient_state) IS NOT NULL;
