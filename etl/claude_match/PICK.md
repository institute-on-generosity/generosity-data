You are doing record linkage for US nonprofit grant data, by your own judgment (do NOT write code or heuristics to decide; read and decide each item yourself).

Input: /Users/hunterzhang/Desktop/generosity-data/data/rematch/pick/chunkNN.json — 500 items. Each item is a grant recipient as a private foundation wrote it on its 990-PF (`grant_recipient`, `state`, `cities`, `grants` = number of grants), plus `candidates`: IRS exempt-org records in that state (ein, legal name, other_name, dba, city, ntee cause code, revenue).

For each item decide which candidate (if any) is the legal entity that would receive and cash that grant.
- Pick a candidate when you are confident it is the same organization: same org written differently (abbreviations, "Inc", "The", "Trustees of X"/"Board of Trustees of X" for X, a well-known public name for its legal name, e.g. "Stanford University" -> "THE BOARD OF TRUSTEES OF THE LELAND STANFORD JUNIOR UNIVERSITY", "Doctors Without Borders" -> "MEDECINS SANS FRONTIERES USA INC").
- Do NOT pick a different entity that shares words: "Friends of X", boosters/PTA/PTO/alumni/auxiliary of X, a chapter vs the national body, a branch office, a bookstore/club/women's club of a university, a "Fund" vs a "Foundation" of the same family, a different school or church with the same generic name.
- Generic names (e.g. "First Baptist Church", "Habitat for Humanity", "Community Foundation", "KIPP"): pick only if the city clearly identifies one candidate; otherwise null.
- Placeholders ("see statement", "individual patient programs", "eligible patients"), individuals' names, and government bodies (school districts, cities, state agencies, public universities that are state entities) -> null.
- If no candidate fits but you confidently know the organization's exact IRS legal name AND it is based in the item's state, put it in `suggest`. Otherwise null. Never guess.
- When unsure, null. Precision matters far more than coverage (target: 95%+ of picks correct).

Output: write a JSON array to /Users/hunterzhang/Desktop/generosity-data/data/rematch/pick/outNN.json with one object per input item, in order: {"id": <id>, "ein": "<9-digit ein or null>", "suggest": "<legal name or null>"}. Compact JSON. You may write it in several parts and concatenate them, but every decision must be your own reading of the item. Other agents run in parallel on other chunks: put any helper script in a file named with your chunk number (e.g. show_NN.py), never a shared name. Read the input in pieces (e.g. python printing 50 items at a time in a compact readable form) so you see every item.

When done, validate with python that outNN.json has 500 entries with ids matching the input, and every ein is null or one of that item's candidate eins. Reply with only: count of picks, count of suggests, count of nulls.
