-- Why a grant can never link to an organization: foreign address, an individual, or a placeholder name
-- ("see statement", "various"). NULL = a US organization (linkable). Lets reports show "linked of linkable".
ALTER TABLE grants ADD COLUMN IF NOT EXISTS recipient_kind text;
UPDATE grants SET recipient_kind = CASE
  WHEN coalesce(recipient_state, '') !~ '^(A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|PR|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])$' THEN 'foreign'
  WHEN recipient_name ~* '(individual|patient|scholarship recipient|eligible|needy|student aid|hip+a|confidential|anonymous)' THEN 'individual'
  WHEN recipient_name ~* '(^\s*(see|refer to)\M|attach|statement|schedule|various|^\s*n/?a\M|^grants?$|not listed|list availab)' THEN 'placeholder'
END
WHERE recipient_ein IS NULL AND recipient_entity IS NULL;
