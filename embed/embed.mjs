// Embed filing_text rows that have no embedding, or one from a different model.
// Usage: node embed.mjs [limit]
import pg from "pg";
import { embed, MODEL_ID, toSql } from "./embedder.mjs";

const limit = Number(process.argv[2] || 1e9);
const BATCH = 32;
const db = new pg.Client({ connectionString: process.env.DATABASE_URL || "postgresql:///nombot" });
await db.connect();
console.log(`model: ${MODEL_ID}`);
const t0 = Date.now();
let done = 0;
while (done < limit) {
  const { rows } = await db.query(
    `SELECT object_id, left(concat_ws(' | ', mission, programs), 2000) AS text FROM filing_text
     WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM $1 LIMIT $2`,
    [MODEL_ID, Math.min(BATCH, limit - done)],
  );
  if (!rows.length) break;
  const vecs = await embed(rows.map((r) => r.text), "document");
  await db.query(
    `UPDATE filing_text f SET embedding = v.e::vector, embedding_model = $3
     FROM unnest($1::text[], $2::text[]) AS v(id, e) WHERE f.object_id = v.id`,
    [rows.map((r) => r.object_id), vecs.map(toSql), MODEL_ID],
  );
  done += rows.length;
  if (done % 1024 < BATCH) console.log(`${done} rows, ${((Date.now() - t0) / 1000).toFixed(0)}s`);
}
console.log(`embedded ${done} rows in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
await db.end();
