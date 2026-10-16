-- Partner API keys for NomBot's /api/v1/* and /mcp (no direct database access for partners).
-- The secret (nb_live_...) is shown once at creation; only its sha256 is stored. key_prefix is for display.
-- Created, listed and revoked with `npm run api-key` in NomBot web.
CREATE TABLE IF NOT EXISTS api_keys (
  id           serial PRIMARY KEY,
  name         text NOT NULL,                    -- partner or person the key belongs to
  key_prefix   text NOT NULL,                    -- first characters of the secret, e.g. nb_live_ab12cd
  key_hash     text NOT NULL UNIQUE,             -- hex sha256 of the full secret
  scopes       text[] NOT NULL DEFAULT '{read}',
  rate_per_min int NOT NULL DEFAULT 60,
  created_at   timestamptz NOT NULL DEFAULT now(),
  revoked_at   timestamptz,
  last_used_at timestamptz
);

-- Usage as daily counts per key, route and HTTP status: one row per combination per day, not one per request.
CREATE TABLE IF NOT EXISTS api_usage (
  key_id int NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
  day    date NOT NULL DEFAULT current_date,
  route  text NOT NULL,                          -- e.g. /api/v1/search, mcp:get_organization
  status int NOT NULL,
  n      int NOT NULL DEFAULT 1,
  PRIMARY KEY (key_id, day, route, status)
);
