// Claude calls for etl/label_causes.py: classify candidate orgs for a cause label, or grade a sample.
//
//   node etl/labels/claude.mjs --mode classify|judge --system sys.txt --in items.jsonl --out out.jsonl \
//        [--model claude-haiku-5-5] [--effort low] [--batch 25] [--concurrency 8] [--api live|batch|cli]
//        [--max-util 0.95] [--max-wait-min 360] [--timeout-min 10]          (cli only)
//
// --api cli runs each batch through Claude Code (`claude -p`) on the Claude subscription instead of the API key:
// same system prompt and JSON schema, no tools, concurrency 3 by default, rate-limit pauses. See claude_cli.mjs.
//
// items.jsonl: one {id, text} per line (text = the org record as the model should read it).
// out.jsonl: one {id, tier, confidence|why, evidence} per line, appended as batches finish, so a rerun
// resumes (ids already in out.jsonl are skipped). Token usage per call goes to out.jsonl + ".usage.jsonl".
// Key: ANTHROPIC_API_KEY, else NomBot's web/.env.local (sibling checkout), like NomBot eval/judge_matches.mts.
import { appendFileSync, existsSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";
import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { betaZodOutputFormat } from "@anthropic-ai/sdk/helpers/beta/zod";
import { z } from "zod";

const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const mode = arg("mode", "classify");
const model = arg("model", mode === "judge" ? "claude-opus-5-5" : "claude-haiku-5-5");
const effort = arg("effort", mode === "judge" ? "medium" : "low");
const size = Number(arg("batch", 25));
const conc = Number(arg("concurrency", 8));
const out = arg("out");
const system = readFileSync(arg("system"), "utf8");

const api = arg("api", "live");
// --api cli bills the Claude subscription through `claude -p` (claude_cli.mjs): no key is loaded or passed on.
if (api !== "cli" && !process.env.ANTHROPIC_API_KEY) {
  for (const p of [join(import.meta.dirname, "..", "..", "..", "NomBot", "web", ".env.local")]) {
    try { process.loadEnvFile(p); break; } catch { /* next */ }
  }
}
const ws = process.env.ANTHROPIC_WORKSPACE_ID;
const client = api === "cli" ? null : new Anthropic(ws ? { defaultHeaders: { "anthropic-workspace-id": ws } } : {});

const tier = z.enum(["primary", "partial", "not"]);
const schema = mode === "judge"
  ? z.object({ results: z.array(z.object({ id: z.string(), tier, why: z.string() })) })
  : z.object({ results: z.array(z.object({ id: z.string(), quote: z.string(), tier, confidence: z.number(), evidence: z.string() })) });

// Fingerprint (fp) = sha256 of the exact text sent, first 16 hex chars. Every result row carries its item's fp, so
// an id is done when its last row's fp matches (rows written before fingerprints count as done); changed text reruns.
const fpOf = (t) => createHash("sha256").update(t).digest("hex").slice(0, 16);
const done = new Map(existsSync(out) ? readFileSync(out, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)).map((r) => [r.id, r.fp]) : []);
const items = readFileSync(arg("in"), "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)).map((x) => ({ ...x, fp: fpOf(x.text) }))
  .filter((x) => !done.has(x.id) || (done.get(x.id) !== undefined && done.get(x.id) !== x.fp));
const fpById = new Map(items.map((x) => [x.id, x.fp]));
const withFp = (r) => ({ ...r, fp: fpById.get(r.id) });
const batches = Array.from({ length: Math.ceil(items.length / size) }, (_, i) => items.slice(i * size, i * size + size));
console.error(`${mode} ${model}: ${items.length} items to do (${done.size} done), ${batches.length} calls`);

// --api cli: Claude Code on the subscription (aliases haiku|sonnet|opus or full model ids).
if (api === "cli") {
  const { runCli } = await import("./claude_cli.mjs");
  await runCli({ mode, model, effort, system, schema, batches, out, withFp, conc: Number(arg("concurrency", 3)),
                 maxUtil: Number(arg("max-util", 0.95)), maxWaitMs: Number(arg("max-wait-min", 360)) * 60_000,
                 timeoutMs: Number(arg("timeout-min", 10)) * 60_000 });
  process.exit(0);
}

async function call(batch) {
  const content = batch.map((x) => `<org id="${x.id}">\n${x.text}\n</org>`).join("\n");
  const base = { model, max_tokens: 8000, system, messages: [{ role: "user", content }] };
  // Haiku: no server-side fallback; Opus/Sonnet: fallbacks on refusal (as in NomBot's judge).
  const res = model.includes("haiku")
    ? await client.messages.parse({ ...base, output_config: { effort, format: zodOutputFormat(schema) } })
    : await client.beta.messages.parse({ ...base, betas: ["server-side-fallback-2026-07-01"], fallbacks: "default",
        output_config: { effort, format: betaZodOutputFormat(schema) } });
  appendFileSync(out + ".usage.jsonl", JSON.stringify({ model: res.model ?? model, n: batch.length, ...res.usage }) + "\n");
  const ids = new Set(batch.map((x) => x.id));
  const rows = (res.parsed_output?.results ?? []).filter((r) => ids.has(r.id));
  if (rows.length) appendFileSync(out, rows.map((r) => JSON.stringify(withFp(r))).join("\n") + "\n");
  return rows.length;
}

// --api batch: Message Batches API (half price, its own rate limits). The batch id is kept in out + ".batch" so a
// rerun resumes polling instead of submitting again; finished results are appended to out like the live path.
if (api === "batch") {
  const idFile = out + ".batch";
  let id = existsSync(idFile) ? readFileSync(idFile, "utf8").trim() : null;
  if (!id && batches.length) {
    const fmt = zodOutputFormat(schema);
    const requests = batches.map((b, i) => ({
      custom_id: `b${i}`,
      params: { model, max_tokens: 8000, system, output_config: { effort, format: { type: fmt.type, schema: fmt.schema } },
                messages: [{ role: "user", content: b.map((x) => `<org id="${x.id}">\n${x.text}\n</org>`).join("\n") }] },
    }));
    id = (await client.messages.batches.create({ requests })).id;
    writeFileSync(idFile, id);
    console.error(`batch ${id}: ${requests.length} requests`);
  }
  if (id) {
    for (;;) {
      const b = await client.messages.batches.retrieve(id);
      if (b.processing_status === "ended") break;
      console.error(`  ${b.processing_status}: ${JSON.stringify(b.request_counts)}`);
      await new Promise((r) => setTimeout(r, 60_000));
    }
    let n = 0, bad = 0;
    const written = new Set();
    for await (const r of await client.messages.batches.results(id)) {
      if (r.result.type !== "succeeded") { bad++; continue; }
      const m = r.result.message;
      appendFileSync(out + ".usage.jsonl", JSON.stringify({ model: m.model, batch: true, ...m.usage }) + "\n");
      try {
        const rows = JSON.parse(m.content.find((c) => c.type === "text")?.text ?? "{}").results ?? [];
        const fresh = rows.filter((x) => fpById.has(x.id) && !written.has(x.id));
        fresh.forEach((x) => written.add(x.id));
        if (fresh.length) appendFileSync(out, fresh.map((x) => JSON.stringify(withFp(x))).join("\n") + "\n");
        n += fresh.length;
      } catch { bad++; }
    }
    unlinkSync(idFile);
    console.error(`done: ${n} results, ${bad} failed requests (rerun to retry the missing ones)`);
  }
  process.exit(0);
}

let next = 0, got = 0, failed = 0;
async function worker() {
  while (next < batches.length) {
    const b = batches[next++];
    for (let attempt = 0; attempt < 3; attempt++) {
      try { const k = await call(b); got += k; break; } catch (e) {
        if (attempt === 2) { failed += b.length; console.error(`batch failed: ${e.message?.slice(0, 200)}`); }
        else await new Promise((r) => setTimeout(r, 2000 * (attempt + 1)));
      }
    }
    if (next % 20 === 0) console.error(`  ${next}/${batches.length} calls, ${got} results`);
  }
}
await Promise.all(Array.from({ length: conc }, worker));
console.error(`done: ${got} results, ${failed} failed`);
