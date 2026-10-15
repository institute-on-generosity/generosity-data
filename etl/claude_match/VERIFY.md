You check record linkage for US nonprofit data, by your own judgment (do NOT write code or heuristics to decide; read every item yourself).

Input: /Users/hunterzhang/Desktop/generosity-data/data/rematch/pick/verifyN.json. Each item pairs a grant recipient (as a foundation wrote it on its 990-PF: grant_recipient, state, cities) with one IRS exempt-org record (irs_name, irs_other_name, irs_dba, irs_city, cause = NTEE code).

Decide if they are the same organization: the IRS record is the legal entity that would receive and cash that grant.
- Same: the same name written differently (abbreviations, "Inc", "The", word order, truncation, Theater/Theatre), a legal name like "Trustees of X" for X, a well-known public name for its legal name, or a name listed as the record's other name / DBA.
- Not the same: a different organization that shares words ("Friends of X", boosters/PTA/alumni/auxiliary/foundation of X when the grant names X itself and X could be its own entity, a different chapter, a parent vs an unrelated local group), a combined recipient naming several orgs, or a different place when the name is generic.
- Be strict: answer false when it is a guess.

Output: write /Users/hunterzhang/Desktop/generosity-data/data/rematch/pick/verified_N.json: a compact JSON array, one object per input item in order: {"id": <id>, "same": true|false}. Other agents run in parallel: name any helper script with your N (e.g. show_vN.py). Read the input in pieces (e.g. 60 items at a time) so you see every item. Validate the output has one entry per input id, in order. Reply with only: count true, count false.
