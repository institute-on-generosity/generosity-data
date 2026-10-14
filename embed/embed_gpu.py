#!/usr/bin/env python3
"""Embed filing_text rows on the Mac's GPU (Apple MPS), for national loads where embed.mjs on the CPU
would take ~17 hours (it does ~7 rows/s; this does several hundred).

Same vectors as embed.mjs's local model (cosine 1.0000, checked with --check), same embedding_model id:
nomic-embed-text-v1.5 at full precision, "search_document: " prefix, mean pooling, layer norm, first 512
dims, L2 norm. (The 8-bit build the apps used before scored only ~0.975 against it, so everything moved
to full precision: documents here, queries in the apps' embedder.)

Setup:  python3 -m venv .venv && .venv/bin/pip install torch sentence-transformers einops
Usage:  .venv/bin/python embed/embed_gpu.py            # every row with no embedding (or another model's)
        .venv/bin/python embed/embed_gpu.py --check    # compare with 20 rows embed.mjs already embedded
"""
import csv, io, os, subprocess, sys, time

import torch
import torch.nn.functional as F
from sentence_transformers import SentenceTransformer

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
MODEL_ID = "local:nomic-embed-text-v1.5-fp32@512"
DIMS, BATCH, WRITE_EVERY = 512, 8, 4096
TEXT = "left(concat_ws(' | ', mission, programs), 2000)"

def psql(sql, stdin=None):
    p = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=stdin if stdin is not None else sql,
                       text=True, capture_output=True)
    if p.returncode:
        sys.exit(f"psql failed: {p.stderr.strip()[:1000]}")
    return p.stdout

def rows(where):
    out = psql(f"\\copy (SELECT object_id, {TEXT}, embedding FROM filing_text WHERE {where}) TO STDOUT WITH (FORMAT csv)")
    return list(csv.reader(io.StringIO(out)))

def embed(model, texts):
    v = model.encode(["search_document: " + t for t in texts], batch_size=BATCH, convert_to_tensor=True, normalize_embeddings=False)
    v = F.layer_norm(v, (v.shape[1],))[:, :DIMS]
    return F.normalize(v, p=2, dim=1).cpu().tolist()

def write(ids, vecs):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for i, v in zip(ids, vecs):
        w.writerow([i, "[" + ",".join(f"{x:.6f}" for x in v) + "]"])
    psql(None, stdin=f"""CREATE TEMP TABLE e (object_id text, v text);
\\copy e FROM STDIN WITH (FORMAT csv)
{buf.getvalue()}\\.
UPDATE filing_text f SET embedding = e.v::vector, embedding_model = '{MODEL_ID}' FROM e WHERE f.object_id = e.object_id;
""")

def main():
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = SentenceTransformer("nomic-ai/nomic-embed-text-v1.5", trust_remote_code=True, device=device)
    print(f"device: {device}", flush=True)
    if "--check" in sys.argv:
        sample = rows(f"embedding_model = '{MODEL_ID}' AND embedding IS NOT NULL ORDER BY object_id LIMIT 20")
        new = embed(model, [r[1] for r in sample])
        old = [[float(x) for x in r[2].strip("[]").split(",")] for r in sample]
        cos = [sum(a * b for a, b in zip(o, n)) for o, n in zip(old, new)]
        print(f"cosine with embed.mjs: min {min(cos):.4f}, mean {sum(cos) / len(cos):.4f}")
        return
    todo = rows(f"embedding IS NULL OR embedding_model IS DISTINCT FROM '{MODEL_ID}'")
    todo.sort(key=lambda r: len(r[1]))  # similar lengths batch together (less padding)
    print(f"{len(todo)} rows to embed", flush=True)
    t0, done = time.time(), 0
    for i in range(0, len(todo), WRITE_EVERY):
        chunk = todo[i:i + WRITE_EVERY]
        write([r[0] for r in chunk], embed(model, [r[1] for r in chunk]))
        if device == "mps": torch.mps.empty_cache()  # long texts late in the run otherwise run out of GPU memory
        done += len(chunk)
        rate = done / (time.time() - t0)
        print(f"{done}/{len(todo)} rows, {rate:.0f}/s, ~{(len(todo) - done) / rate / 60:.0f} min left", flush=True)

if __name__ == "__main__":
    main()
