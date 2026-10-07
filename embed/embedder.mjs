// Provider-agnostic 512-dim text embeddings.
//   EMBED_PROVIDER=openai  -> text-embedding-3-small (dimensions: 512), needs OPENAI_API_KEY
//   EMBED_PROVIDER=local   -> LOCAL FALLBACK for the POC only: nomic-embed-text-v1.5 run on
//                             this machine (Matryoshka-truncated 768 -> 512). Not comparable
//                             with OpenAI vectors; rows record their model in embedding_model.
// Default: openai when OPENAI_API_KEY is set, otherwise local.
import { embedMany } from "ai";
import { openai } from "@ai-sdk/openai";

export const DIMS = 512;
const provider = process.env.EMBED_PROVIDER || (process.env.OPENAI_API_KEY ? "openai" : "local");
export const MODEL_ID = provider === "openai" ? "openai:text-embedding-3-small@512" : "local:nomic-embed-text-v1.5@512";

let extractor;
async function local(texts, kind) {
  if (!extractor) {
    const { pipeline } = await import("@huggingface/transformers");
    extractor = await pipeline("feature-extraction", "nomic-ai/nomic-embed-text-v1.5", { dtype: "q8" });
  }
  const prefix = kind === "query" ? "search_query: " : "search_document: ";
  const out = await extractor(texts.map((t) => prefix + t), { pooling: "mean" });
  return out.tolist().map((v) => {
    // Matryoshka: layer-norm, truncate to 512, L2-normalize (per nomic's guidance)
    const mean = v.reduce((a, b) => a + b, 0) / v.length;
    const sd = Math.sqrt(v.reduce((a, b) => a + (b - mean) ** 2, 0) / v.length) || 1;
    const t = v.map((x) => (x - mean) / sd).slice(0, DIMS);
    const norm = Math.hypot(...t) || 1;
    return t.map((x) => x / norm);
  });
}

export async function embed(texts, kind = "document") {
  if (provider === "openai") {
    const { embeddings } = await embedMany({
      model: openai.embedding("text-embedding-3-small"),
      values: texts,
      providerOptions: { openai: { dimensions: DIMS } },
    });
    return embeddings;
  }
  return local(texts, kind);
}

export const toSql = (v) => `[${v.map((x) => x.toFixed(6)).join(",")}]`;
