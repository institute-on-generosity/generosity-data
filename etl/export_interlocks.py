#!/usr/bin/env python3
"""Export high-confidence hidden board interlocks for the graph view (docs/board-interlocks/index.html).

  python3 etl/export_interlocks.py      # writes docs/board-interlocks/interlocks.json (~6 MB, git-ignored)

Keeps relation = 'link' (no sister orgs or bank trusts) and confidence = 'high'. The page fetches the
JSON from its own folder, so open it through a local server: python3 -m http.server -d docs/board-interlocks
Format: {"n": [[ein, name, is_foundation, city, state]], "e": [[a, b, kind, shared, people, amount, grants]]},
kind 0 funder-grantee, 1 funder-funder, 2 funder-nonprofit, 3 nonprofit-nonprofit.
"""
import json, os, subprocess

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "board-interlocks", "interlocks.json")
SQL = """
SELECT i.ein_a, i.ein_b, coalesce(fa.name, oa.name, i.ein_a), coalesce(fb.name, ob.name, i.ein_b),
       (fa.ein IS NOT NULL)::int, (fb.ein IS NOT NULL)::int, coalesce(oa.city, fa.city, ''), coalesce(ob.city, fb.city, ''), i.state,
       CASE i.kind WHEN 'funder-grantee' THEN 0 WHEN 'funder-funder' THEN 1 WHEN 'funder-nonprofit' THEN 2 ELSE 3 END,
       i.shared, array_to_string(i.people[1:4], ', '), coalesce(round(i.amount)::bigint, 0), coalesce(i.grants, 0)
FROM board_interlocks i
LEFT JOIN orgs oa ON oa.ein = i.ein_a LEFT JOIN funders fa ON fa.ein = i.ein_a
LEFT JOIN orgs ob ON ob.ein = i.ein_b LEFT JOIN funders fb ON fb.ein = i.ein_b
WHERE i.relation = 'link' AND i.confidence = 'high'
"""

def main():
    rows = subprocess.run(["psql", DB, "-At", "-F", "\t", "-c", SQL], capture_output=True, text=True, check=True).stdout.splitlines()
    index, nodes, edges = {}, [], []
    def node(ein, name, funder, city, state):
        if ein not in index:
            index[ein] = len(nodes); nodes.append([ein, name.strip(), funder, city.strip().title(), state])
        elif funder:
            nodes[index[ein]][2] = 1
        return index[ein]
    for line in rows:
        a, b, na, nb, fa, fb, ca, cb, st, kind, shared, people, amount, grants = line.split("\t")
        edges.append([node(a, na, int(fa), ca, st), node(b, nb, int(fb), cb, st), int(kind), int(shared), people.title(), int(amount), int(grants)])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"n": nodes, "e": edges}, f, separators=(",", ":"))
    print(f"{len(nodes)} orgs, {len(edges)} links -> {os.path.normpath(OUT)}")

if __name__ == "__main__":
    main()
