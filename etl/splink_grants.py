#!/usr/bin/env python3
"""Splink matching for grants still missing a recipient: a probabilistic model that weighs name, city, ZIP
and entity-type words together, learned from the data (EM), not hand-set rules. Writes proposals into rm_cand
as method 'splink' (score = match probability) so etl/rematch_grants.py can sample and apply them.

  .venv-analytics/bin/python etl/splink_grants.py        # rebuild rm_u, train, predict, write rm_cand
  python3 etl/rematch_grants.py --sample=60              # then grade (NomBot eval/judge_matches.mts)

Runs on DuckDB in memory; needs .venv-analytics (splink 4).
"""
import os, subprocess, sys, time
import duckdb
from splink import DuckDBAPI, Linker, SettingsCreator, block_on
import splink.comparison_library as cl

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
WORK = os.path.join(os.path.dirname(__file__), "..", "data", "rematch", "splink")
sys.path.insert(0, os.path.dirname(__file__))
from rematch_grants import US, SKIP, psql  # noqa: E402

# Words that mark a separate entity sharing the host's name ("X PTA" is not "X")
MARKER = r"\m(PTA|PTO|PTSA|BOOSTERS?|ALUMNI|AUXILIARY|FRIENDS OF|PARENTS?|FOUNDATION|ENDOWMENT|GUILD|CHAPTER)\M"

def export():
    os.makedirs(WORK, exist_ok=True)
    psql(f"""
SET work_mem = '1GB';
DROP TABLE IF EXISTS rm_u;
CREATE UNLOGGED TABLE rm_u AS
SELECT org_norm(recipient_name) AS n, min(recipient_name) AS raw, upper(coalesce(recipient_city, '')) AS city,
       recipient_state AS st, left(coalesce(recipient_zip, ''), 5) AS zip, count(*)::int AS grants, coalesce(sum(amount), 0) AS amt
FROM grants
WHERE recipient_ein IS NULL AND recipient_state ~ {US} AND recipient_name !~* {SKIP}
GROUP BY 1, 3, 4, 5;
DELETE FROM rm_u WHERE length(n) < 4;
CREATE INDEX ON rm_u (zip); CREATE INDEX ON rm_u (st, n); ANALYZE rm_u;
DELETE FROM rm_cand WHERE method = 'splink';
""")
    q = lambda sql, f: subprocess.run(["psql", DB, "-qc", f"\\copy ({sql}) TO '{os.path.join(WORK, f)}' CSV HEADER"], check=True)
    # Left: unmatched recipients. Right: every org, once by IRS name and once by secondary name.
    q(f"""SELECT row_number() OVER () AS unique_id, n, city, st, zip,
             regexp_replace(n, '^(\\S+).*', '\\1') AS w1, substring(n FROM '{MARKER}') AS marker FROM rm_u""", "u.csv")
    q(f"""SELECT ein || k AS unique_id, ein, x AS n, city, st, zip,
             regexp_replace(x, '^(\\S+).*', '\\1') AS w1, substring(x FROM '{MARKER}') AS marker
          FROM rm_o, LATERAL (VALUES ('n', n), ('s', sn)) v(k, x) WHERE x IS NOT NULL""", "o.csv")

def model():
    con = duckdb.connect()
    con.execute("SET threads = 10; SET memory_limit = '14GB'")
    for t in ("u", "o"):
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv('{WORK}/{t}.csv', all_varchar = true)")
    db = DuckDBAPI(connection=con)
    settings = SettingsCreator(
        link_type="link_only",
        unique_id_column_name="unique_id",
        comparisons=[
            cl.JaroWinklerAtThresholds("n", [0.97, 0.92, 0.85]).configure(term_frequency_adjustments=True),
            cl.ExactMatch("city"),
            cl.ExactMatch("zip"),
            cl.ExactMatch("marker"),
        ],
        blocking_rules_to_generate_predictions=[
            block_on("zip", "w1"),
            block_on("st", "city", "w1"),
            block_on("st", "n"),
        ],
        retain_intermediate_calculation_columns=False,
    )
    linker = Linker(["u", "o"], settings, db_api=db)
    t0 = time.time()
    linker.training.estimate_probability_two_random_records_match([block_on("st", "n")], recall=0.7)
    linker.training.estimate_u_using_random_sampling(max_pairs=5e6)
    linker.training.estimate_parameters_using_expectation_maximisation(block_on("zip"))
    linker.training.estimate_parameters_using_expectation_maximisation(block_on("st", "w1"))
    print(f"trained ({time.time() - t0:.0f}s)")
    linker.misc.save_model_to_json(os.path.join(WORK, "model.json"), overwrite=True)
    pred = linker.inference.predict(threshold_match_probability=0.5)
    print(f"predicted ({time.time() - t0:.0f}s)")
    # Best org per recipient, only when clearly ahead of the runner-up (a different EIN)
    con.execute(f"""
      COPY (
        WITH p AS (SELECT p.unique_id_l AS uid, o.ein, max(p.match_probability) AS prob
                   FROM {pred.physical_name} p JOIN o ON o.unique_id = p.unique_id_r GROUP BY 1, 2),
             r AS (SELECT *, row_number() OVER w AS rk, lead(prob) OVER w AS next_p FROM p
                   WINDOW w AS (PARTITION BY uid ORDER BY prob DESC))
        SELECT u.n, u.city, u.st, u.zip, r.ein, 'splink' AS method, r.prob AS score
        FROM r JOIN u ON u.unique_id = r.uid
        WHERE rk = 1 AND (next_p IS NULL OR prob - next_p >= 0.1)
      ) TO '{WORK}/cand.csv' (HEADER)""")
    print(con.execute(f"SELECT count(*) FROM read_csv('{WORK}/cand.csv')").fetchone()[0], "proposals")

def load():
    subprocess.run(["psql", DB, "-qc", f"\\copy rm_cand (n, city, st, zip, ein, method, score) FROM '{WORK}/cand.csv' CSV HEADER"], check=True)
    print(psql("""SELECT 'splink: ' || count(*) || ' names, ' || sum(u.grants) || ' grants, $' || round(sum(u.amt) / 1e6) || 'M'
                  FROM rm_cand c JOIN rm_u u USING (n, city, st, zip) WHERE c.method = 'splink'"""))
    print(psql("""SELECT width_bucket(score, 0.5, 1.0001, 5) b, count(*) FROM rm_cand WHERE method = 'splink' GROUP BY 1 ORDER BY 1"""))

if __name__ == "__main__":
    if "--no-export" not in sys.argv: export()
    model()
    load()
