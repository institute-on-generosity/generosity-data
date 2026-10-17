// --api cli for claude.mjs: run each batch through Claude Code (`claude -p`) so bulk labeling bills the Claude
// subscription (claude.ai login), never an API key.
//
// Billing guard: the child gets ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN and other provider/credential variables
// removed, and every call's stream-json "init" event must report apiKeySource = "none" (OAuth login). Any other
// value aborts the run before a second call is made. Check by hand with:
//   env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN claude auth status      # authMethod "claude.ai", subscriptionType
//   jq -c 'select(.api=="cli") | .apiKeySource' <out>.usage.jsonl | sort | uniq -c   # all "none"
//
// Limits: each call's rate_limit_event (five_hour / seven_day utilization) is logged. A rejected call (usage limit,
// 429) pauses every worker until the window resets (or with backoff when no reset time is given), then resumes; done
// ids are skipped on rerun anyway. --max-util stops the run once the seven-day window passes that share, and
// --max-wait caps a single pause (longer: stop and rerun later).
import { spawn } from "node:child_process";
import { appendFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";

const STRIP = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_WORKSPACE_ID"];
const LIMIT_RE = /usage limit|rate.?limit|limit reached|limit will reset|too many requests|\b429\b|overloaded|\b529\b/i;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function runCli({ mode, model, effort, system, schema, batches, out, withFp, conc, maxUtil, maxWaitMs, timeoutMs }) {
  const jsonSchema = z.toJSONSchema(schema);
  delete jsonSchema.$schema;
  const env = { ...process.env };
  for (const k of STRIP) delete env[k];
  const cwd = mkdtempSync(join(tmpdir(), "label-cli-")); // empty dir: no project CLAUDE.md or settings
  const args = ["-p", "--model", model, "--effort", effort, "--output-format", "stream-json", "--verbose",
    "--tools", "", "--system-prompt", system, "--json-schema", JSON.stringify(jsonSchema),
    "--no-session-persistence", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands"];
  const log = (row) => appendFileSync(out + ".usage.jsonl", JSON.stringify({ api: "cli", ts: new Date().toISOString(), ...row }) + "\n");

  const t0 = Date.now();
  const S = { calls: 0, results: 0, failed: 0, limitHits: 0, waitedMs: 0, stop: null, lastLimit: null };
  let pauseUntil = 0;

  function once(content) {
    return new Promise((resolve) => {
      const child = spawn("claude", args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
      let buf = "", err = "", init = null, result = null, rate = null;
      const timer = setTimeout(() => child.kill("SIGTERM"), timeoutMs);
      child.stdout.on("data", (d) => {
        buf += d;
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i); buf = buf.slice(i + 1);
          if (!line.trim()) continue;
          let ev; try { ev = JSON.parse(line); } catch { continue; }
          if (ev.type === "system" && ev.subtype === "init") {
            init = ev;
            if (ev.apiKeySource !== "none") child.kill("SIGTERM"); // not the subscription login: stop at once
          } else if (ev.type === "rate_limit_event") rate = ev.rate_limit_info;
          else if (ev.type === "result") result = ev;
        }
      });
      child.stderr.on("data", (d) => { err += d; });
      child.on("close", (code) => { clearTimeout(timer); resolve({ code, init, result, rate, err: err.slice(-500) }); });
      child.stdin.end(content);
    });
  }

  async function waitPause() {
    while (Date.now() < pauseUntil && !S.stop) await sleep(Math.min(30_000, pauseUntil - Date.now()));
  }

  async function call(batch) {
    const content = batch.map((x) => `<org id="${x.id}">\n${x.text}\n</org>`).join("\n");
    for (let attempt = 0; attempt < 6 && !S.stop; attempt++) {
      await waitPause();
      if (S.stop) return 0;
      const t = Date.now();
      const r = await once(content);
      S.calls++;
      const wall = Date.now() - t;
      if (r.init && r.init.apiKeySource !== "none") {
        S.stop = `billing: apiKeySource=${JSON.stringify(r.init.apiKeySource)}, not the subscription login`;
        log({ event: "abort", reason: S.stop });
        return 0;
      }
      const u = r.result?.usage ?? {};
      const win = r.rate?.unifiedWindows ?? {};
      const util = { five_hour: win.five_hour?.utilization ?? null, seven_day: win.seven_day?.utilization ?? null };
      if (r.rate) S.lastLimit = { status: r.rate.status, ...util };
      const text = `${r.result?.result ?? ""} ${r.err}`;
      const limited = !!(r.rate?.status === "rejected" || r.result?.api_error_status === 429
        || ((r.result?.is_error || r.code) && LIMIT_RE.test(text)));
      log({ model: r.init?.model ?? model, n: batch.length, apiKeySource: r.init?.apiKeySource ?? null,
            input_tokens: u.input_tokens, output_tokens: u.output_tokens,
            cache_read_input_tokens: u.cache_read_input_tokens, cache_creation_input_tokens: u.cache_creation_input_tokens,
            notional_usd: r.result?.total_cost_usd, duration_ms: r.result?.duration_ms, wall_ms: wall,
            ok: !!r.result && !r.result.is_error, limited, rate_status: r.rate?.status ?? null, ...util,
            error: r.result?.is_error || r.code ? text.slice(0, 300) : undefined });
      if (limited) {
        S.limitHits++;
        const resetsAt = (r.rate?.status === "rejected" && r.rate.resetsAt) ? r.rate.resetsAt * 1000 : 0;
        const wait = resetsAt > Date.now() ? resetsAt - Date.now() + 60_000 : Math.min(30 * 60_000, 60_000 * 2 ** Math.min(5, S.limitHits - 1));
        log({ event: "limit", wait_ms: wait, rate_type: r.rate?.rateLimitType ?? null, resets_at: resetsAt || null });
        if (wait > maxWaitMs) { S.stop = `limit: next reset in ${(wait / 3.6e6).toFixed(1)} h (> --max-wait)`; return 0; }
        console.error(`  limit hit (${r.rate?.rateLimitType ?? "?"}): pausing ${(wait / 60000).toFixed(1)} min`);
        if (Date.now() + wait > pauseUntil) { S.waitedMs += Math.max(0, Date.now() + wait - Math.max(pauseUntil, Date.now())); pauseUntil = Date.now() + wait; }
        attempt--; // limit pauses don't use up retries
        continue;
      }
      if (util.seven_day != null && util.seven_day >= maxUtil) S.stop = `seven-day utilization ${util.seven_day} >= --max-util ${maxUtil}`;
      // structured output, else the result text as JSON
      let parsed = r.result?.structured_output;
      if (!parsed && r.result?.result) { try { parsed = JSON.parse(r.result.result.replace(/^```(json)?|```$/g, "")); } catch { /* retry */ } }
      const ok = schema.safeParse(parsed);
      if (!ok.success) { await sleep(3000 * (attempt + 1)); continue; }
      const ids = new Set(batch.map((x) => x.id));
      const rows = ok.data.results.filter((x) => ids.has(x.id));
      if (rows.length) appendFileSync(out, rows.map((x) => JSON.stringify(withFp(x))).join("\n") + "\n");
      return rows.length;
    }
    S.failed += batch.length;
    return 0;
  }

  let next = 0;
  async function worker() {
    while (next < batches.length && !S.stop) {
      const i = next++;
      const k = await call(batches[i]);
      S.results += k;
      if ((i + 1) % 10 === 0) console.error(`  ${i + 1}/${batches.length} batches, ${S.results} results, ${((Date.now() - t0) / 60000).toFixed(1)} min, `
        + `limits ${JSON.stringify(S.lastLimit)}`);
    }
  }
  await Promise.all(Array.from({ length: conc }, worker));
  const wall = Date.now() - t0;
  log({ event: "run", mode, model, effort, conc, batches: batches.length, items: batches.reduce((a, b) => a + b.length, 0),
        calls: S.calls, results: S.results, failed: S.failed, limit_hits: S.limitHits, waited_ms: S.waitedMs, wall_ms: wall,
        orgs_per_hour: Math.round(S.results / (wall / 3.6e6)), last_limit: S.lastLimit, stopped: S.stop });
  console.error(`done (cli): ${S.results} results, ${S.failed} failed, ${S.calls} calls, ${(wall / 60000).toFixed(1)} min, `
    + `${S.limitHits} limit hits${S.stop ? `; stopped: ${S.stop}` : ""}`);
  if (S.stop?.startsWith("billing")) process.exit(2);
}
