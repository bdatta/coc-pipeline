# MongoDB Schema — COC Knowledge Base

Three collections in one database. Sample documents in this folder are in
MongoDB Extended JSON v2, so they import directly:

```bash
mongoimport --uri "$MONGODB_URI" --db coc_rag --collection coc_chunks \
  --file coc_chunks.sample.json --jsonArray --drop
```

> The `embedding` arrays in the samples are truncated to 8 floats for
> readability. Real vectors are 1024 floats for `voyage-3.5`. If you import a
> sample and then create the vector index, the dimension mismatch will make
> those rows unqueryable — import samples to *inspect* the shape, not to search.

---

## 1. `coc_chunks` — the retrieval collection

**Purpose:** one document per retrievable chunk. This is the only collection
`$vectorSearch` touches. Everything needed to retrieve, filter, rank, and cite a
chunk lives in the same document as its vector.

**Why the vector is co-located rather than in its own collection:** `$vectorSearch`
operates on a single collection and returns whole documents. Splitting vectors
from text would force a `$lookup` on every query — an extra stage, an extra round
trip — and you would still have to duplicate the filter fields alongside the
vectors for pre-filtering to work. There is no upside.

The cost is document weight: 1024 float64s is roughly 8 KB. That is why every
read path in `vectorstore.py` uses an explicit projection that omits `embedding`.
A `find()` that forgets to do this pulls 8 KB per document for nothing.

### Field groups

| Group | Fields | Why it exists |
|---|---|---|
| **Identity** | `_id`, `doc_id`, `seq`, `type` | `_id` *is* the deterministic chunk ID, so re-running the pipeline upserts in place instead of duplicating. `seq` gives document order, which is what makes neighbour expansion possible. |
| **Section context** | `heading`, `section_path`, `breadcrumb` | The mechanism that keeps a benefit value attached to its meaning. `section_path` is an array so Atlas can filter on any single level of the hierarchy. |
| **Content** | `text`, `embed_text` | `text` is the raw chunk for display; `embed_text` is what was actually vectorized (breadcrumb + table + row details). Deliberately redundant — when a result looks wrong, you need to see exactly what the vector represents. |
| **Provenance** | `page_start`, `page_end`, `source_filename`, `source_sha256`, `pipeline_version`, `chunk_config_hash`, `created_at`, `updated_at` | Page numbers make answers citable. The SHA tells you whether a re-upload is genuinely a new document. The version and config hash let you find chunks produced by superseded logic — see `stale_chunks()`. |
| **Plan identity** | `plan_id`, `group_number`, `plan_year`, `effective_date`, `termination_date`, `carrier`, `market_segment`, `states` | **The most important group to get right before loading production data.** Without these you cannot answer "what was the copay under the 2025 plan," and you cannot safely hold two carriers in one collection. |
| **Table detail** | `table_id`, `part_index`, `part_total`, `n_rows`, `n_cols`, `column_labels`, `has_spans` | `table_id` + `part_index` reassembles a split table. `column_labels` drives display and feeds the lexical index. `has_spans` marks the chunks where extraction was hardest. |
| **Embedding lifecycle** | `embedding`, `embed_model`, `embed_dim`, `embedded_at` | Lets you migrate models incrementally — query for everything not yet embedded with the new model instead of re-embedding the whole corpus. |
| **Review workflow** | `needs_review`, `extraction_notes` | The hook for human review. Currently set when a table chunk's empty-cell ratio exceeds a threshold, which is the classic symptom of a mis-detected grid. |

### On plan identity and filters

`plan_id` and `plan_year` are declared as `filter` fields on the vector index, so
they apply as a **pre-filter inside** `$vectorSearch` rather than after the fact.
This matters for correctness, not just speed: answering from the wrong plan year
is the highest-consequence failure this system has, and a post-filter can return
fewer than `k` results while a pre-filter searches only the right partition.

```javascript
{ $vectorSearch: {
    index: "coc_vector_index",
    path: "embedding",
    queryVector: [...],
    numCandidates: 200,
    limit: 8,
    filter: { plan_id: { $eq: "ACME-PPO-2000" }, plan_year: { $eq: 2026 } }
}}
```

---

## 2. `coc_documents` — the document record

**Purpose:** one record per COC, holding the converted Markdown, the table
structures with true rowspan/colspan, the conversion settings used, and the plan
identity that every chunk inherits.

**Why it's a separate collection:** it makes "Markdown as a checkpoint" real. You
can re-chunk with different parameters — different chunk size, overlap, or
row-sentence setting — without re-parsing 200 pages of PDF. It also keeps the
audit artifact in the same database as the chunks it produced, so tracing an
answer back to a source cell doesn't mean hunting for a file someone downloaded
six months ago.

It is small: one document per COC, not one per chunk. Keeping it out of
`coc_chunks` means the multi-megabyte Markdown blob never gets pulled into a
vector search result.

**Key fields beyond plan identity:**

- `markdown` — the full converted document
- `tables` — every cell with its true span (the fidelity audit trail)
- `tables_complete` — `false` when the record was stored without table
  structures, so an incomplete audit trail is visible in the data rather than
  indistinguishable from a document that genuinely had no tables
- `conversion_options` / `conversion_stats` — exactly which settings produced this output, and what they found
- `chunk_options` / `chunk_config_hash` — reproducibility of the chunking step
- `source_sha256` — recognise a re-upload of an already-ingested document
- `status` — `converted` / `chunked` / `indexed` / `failed`

**The audit join is `coc_chunks.table_id` → `coc_documents.tables[].table_id`.**
Chunks carry `table_id`, `column_labels`, and `has_spans` — enough to know a
table had merged cells — but the cell-level spans live only here, because that
array can run to thousands of entries and `coc_chunks` is read on every search.

```javascript
db.coc_documents.aggregate([
  { $match: { _id: "hsa_spd_2025" } },
  { $unwind: "$tables" },
  { $match: { "tables.table_id": "tbl-0007" } },
  { $project: { _id: 0, table: "$tables" } }
])
```

Phase 2 reconciles the table IDs in the Markdown against the uploaded structures
before storing. A mismatch means the two files came from different conversion
runs, which would point the audit trail at the wrong cells, so the structures are
rejected rather than stored.

**Watch the 16 MB BSON limit.** A 200-page COC is typically 1–3 MB of Markdown,
so there is ample headroom. If you approach it, gzip the Markdown and store it as
`BinData` rather than splitting the record.

---

## 3. `coc_query_log` — optional, but build it early

**Purpose:** one record per question asked: the query, the retrieval settings,
what came back with scores, the generated answer, and any user feedback.

**Why it's worth the trouble:** this is the raw material for the evaluation set.
Hybrid search, reranking, chunk-size changes, and a different embedding model all
*feel* like improvements. Without a scoreboard you cannot tell which ones were.
Logging real questions from day one means that when you sit down to build a
30–50 question eval set, you already have real ones instead of invented ones.

It also gives you the failure archaeology you'll want: when someone reports a
wrong answer, you can see exactly which chunks were retrieved, at what scores,
under which filters and which embedding model.

**Set a TTL index** if retention is a concern — these accumulate fast and the
free tier has 512 MB total:

```javascript
db.coc_query_log.createIndex({ asked_at: 1 }, { expireAfterSeconds: 7776000 })  // 90 days
```

If questions could contain member-identifying information, treat this collection
as sensitive: restrict access, or hash/redact `user_id` and the question text.

---

## Indexes

Full definitions are in `search_indexes.json`. Two things to note:

**`numDimensions` must match your embedding model.** Change the model, and the
index has to be rebuilt — one more reason `embed_model` is stored on every chunk.

**Free-tier clusters allow only a small number of search indexes.** The vector
index is mandatory; the lexical index for hybrid retrieval costs a second one.
Add it deliberately, when you have evidence that exact-term lookups (CPT codes,
defined plan terms) are failing.

---

## What deliberately isn't here

- **A collection per COC.** Vector search indexes are per-collection and the free
  tier allows very few. One collection filtered by `doc_id` / `plan_id` is the
  only shape that scales past a couple of documents.
- **A separate embeddings collection.** See the co-location note above.
- **Normalized section documents.** The heading hierarchy is denormalized onto
  every chunk as `section_path`. It's duplicated storage, but it makes filtering
  and citation work without a join, and sections are immutable once extracted.
