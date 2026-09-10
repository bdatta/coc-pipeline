# CLI Runbook — Step by Step

Every job in order, with all parameters and the check to run after each one.
Nine steps from a PDF to a queryable index in Atlas.

Each step is safe to repeat. Nothing before step 8 touches Atlas or costs money,
so iterate freely on steps 2–4 until the output is right — that is far cheaper
than discovering a problem after embedding.

Windows paths use `\`; on macOS/Linux use `/`.

---

## Step 0 — Environment check

**Job:** `tools/doctor.py`

```bash
python tools/doctor.py
python tools/doctor.py --config pipeline.yaml
```

| Parameter | Meaning |
|---|---|
| `--config PATH` | Also validate a pipeline YAML and report its key settings |

**Checkpoint.** Every line reads `OK`. Specifically: Python 3.10+, `pdfplumber`,
`yaml`, `dotenv`, `pymongo`, an embedding provider, `MONGODB_URI` and an API key
set, and Atlas reachable.

Run this first on any new machine. It separates missing packages from missing
credentials from an unreachable cluster, so a failure points at one thing.

---

## Step 1 — Inspect the PDF before converting

Not a tool, but two minutes here saves re-running everything later.

```bash
python -c "import pdfplumber; p=pdfplumber.open('coc.pdf'); print('pages:', len(p.pages)); [print(f'  p{i+1}:', (p.pages[i].extract_text() or '')[:60].replace(chr(10),' ')) for i in range(0,6)]"
```

**Checkpoint.** Text appears. If pages come back empty, the PDF is scanned and
has no text layer — this pipeline cannot read it and you need OCR first.

Note which PDF page real content starts on. You will pass it as `--first`.

---

## Step 2 — Convert

**Job:** `tools/convert.py`

```bash
python tools/convert.py coc.pdf \
    -o build/coc.md \
    --tables-json build/coc.tables.json \
    --first 3 \
    --last 185
```

| Parameter | Default | Meaning |
|---|---|---|
| `pdf` | required | Source PDF |
| `-o`, `--out PATH` | alongside the PDF | Output Markdown |
| `--first N` | auto-detect | First PDF page. **Set explicitly for a bound compilation** — auto-detection finds the largest consistently numbered block, not the content start |
| `--last N` | last page | Last PDF page |
| `--tables-json PATH` | not written | Cell-level table export with true rowspan/colspan. Needed in step 8 for the audit trail |
| `--bold-headings` | off | Treat bold short lines as headings. Leave off unless headings are being missed — on, bold cross-references get promoted |
| `--allcaps-headings` | off | Treat ALL CAPS lines as headings |

**Checkpoint.** Read the stats it prints:

```
   pages                    183
   headings                 277      <- not 0, not thousands
   tables                   18
   tables_spanning_pages    7        <- >0 if tables cross pages
   form_sections            0        <- >0 only for bound compilations
   running_lines_removed    9
```

If it warns that page-numbering confidence is below 90%, the document restarts
its numbering — re-run with `--first` set explicitly.

---

## Step 3 — Validate the conversion

**Job:** `tools/validate.py`

```bash
python tools/validate.py build/coc.md \
    --pdf coc.pdf \
    --toc toc.txt \
    --json build/coc.validation.json
```

| Parameter | Default | Meaning |
|---|---|---|
| `markdown` | required | Converted Markdown |
| `--pdf PATH` | none | Source PDF. **Supply it** — text recall is the only check the converter cannot fake |
| `--toc PATH` | none | Table-of-contents text file; enables the section coverage check |
| `--json PATH` | none | Write the full report |
| `--quiet` | off | Verdict line only |

**Checkpoint.** `VERDICT: PASS` and `blockers: 0`. Exit code `0` pass, `1`
review, `2` quarantine.

Also read the `form_sections` finding: for a bound compilation it should list
each document and its printed page range.

**Do not continue on a quarantine.** Fix the conversion settings and return to
step 2 — do not hand-edit the Markdown, it is derived and the edit will be lost.

---

## Step 4 — Check the section structure

**Job:** `tools/inventory.py`

```bash
python tools/inventory.py build/coc.md --divisions
python tools/inventory.py build/coc.md --toc toc.txt
python tools/inventory.py build/coc.md --all-levels
```

| Parameter | Meaning |
|---|---|
| `markdown` | Converted Markdown |
| `--divisions` | List only named divisions with their heading depth |
| `--toc PATH` | Report which ToC entries were detected as headings |
| `--all-levels` | Every heading, no truncation |

**Checkpoint.** In `--divisions`, every entry shows **H1**. Anything flagged
`<< nested at depth N` means a subsection replaced its parent on the heading
stack, and every chunk beneath it will carry the wrong breadcrumb.

With `--toc`, all entries should match. A `MISS` means the heading was not
detected and its content was absorbed into the preceding section.

Also read the duplicate-breadcrumb warning: two sections with the same path are
indistinguishable at retrieval time.

---

## Step 5 — Chunk

**Job:** `tools/chunk.py`

```bash
python tools/chunk.py build/coc.md \
    -o build/coc.chunks.jsonl \
    --doc-id acme_ppo_2026 \
    --pdf coc.pdf \
    --plan-id ACME-PPO-2000 \
    --group-number 0084512 \
    --plan-year 2026 \
    --effective-date 2026-01-01 \
    --carrier "Acme Health" \
    --market-segment large_group \
    --states IL,IN
```

| Parameter | Default | Meaning |
|---|---|---|
| `markdown` | required | Converted Markdown |
| `-o`, `--out PATH` | `<md>.chunks.jsonl` | Output JSONL |
| `--doc-id ID` | Markdown file stem | **Namespaces everything.** Keep it stable across re-runs of the same document; make it different per plan and plan year |
| `--pdf PATH` | none | Records `source_sha256`, so a re-upload can be recognised |
| `--plan-id`, `--group-number`, `--plan-year` | none | Plan identity. `plan_id` and `plan_year` become vector-index filters |
| `--effective-date`, `--market-segment`, `--carrier`, `--states` | none | Further plan metadata |
| `--target-tokens N` | 650 | Target size of a text chunk |
| `--max-tokens N` | 900 | Hard ceiling |
| `--overlap-tokens N` | 90 | Overlap carried between chunks |
| `--max-table-tokens N` | 1100 | Ceiling for a table chunk before it splits by rows |
| `--no-row-sentences` | off | Omit the `Column: value` expansion from table chunks |
| `--stats-only` | off | Report without writing the JSONL |

**Checkpoint.**

```
chunks            335  (298 text, 37 table)
tokens            median 257, p90 707, max 1345
total tokens      121,959          <- one-time embedding cost
distinct tables   18  (6 split across parts)
flagged review    14
atlas storage     ~2.6 MB at 1024 dims
```

Median well under `max_tokens`, and `flagged review` a small fraction. Note the
split tables — they are the ones to audit next.

Run with `--stats-only` first to size the job before writing anything.

---

## Step 6 — Audit split tables

**Job:** `tools/table_audit.py`

```bash
python tools/table_audit.py --from-jsonl build/coc.chunks.jsonl
python tools/table_audit.py --from-jsonl build/coc.chunks.jsonl --table-id tbl-0002
python tools/table_audit.py --from-jsonl build/coc.chunks.jsonl --table-id tbl-0002 --reassembled
```

| Parameter | Meaning |
|---|---|
| `--from-jsonl PATH` | Read from the chunk export (before embedding) |
| `--doc-id ID` | Read from Atlas instead (after embedding) |
| `--table-id ID` | Detail for one table; omit to list all |
| `--reassembled` | Print the table stitched back together |
| `--show-text` | Print each part's embedded text |

**Checkpoint.** `N of N tables reassemble cleanly`. Any `!!` row means a missing
part or a header that drifts between parts — the latter would put rows under the
wrong column labels.

For the largest table, run `--reassembled` and compare against the PDF. This is
the one check no tooling can do for you.

---

## Step 7 — Spot-check denial lookups (optional but recommended)

**Job:** `tools/diagnose_denial.py`

```bash
python tools/diagnose_denial.py build/coc.md E0240 "Bath or shower chair" "bath chair,bath seat"
python tools/diagnose_denial.py build/coc.md S4025 "Donor services for IVF" \
    --diagnosis Z31.9 --diagnosis-desc "Encounter for procreative management"
python tools/diagnose_denial.py build/coc.md --batch cases.csv
```

| Parameter | Meaning |
|---|---|
| `markdown` | Converted Markdown (optional with `--atlas`) |
| `code`, `descriptor`, `synonyms` | Single-case positional arguments |
| `--diagnosis`, `--diagnosis-desc` | Optional diagnosis code and its descriptor |
| `--batch PATH` | Run a whole case CSV |
| `--atlas` | Search the Atlas index instead of the Markdown |
| `--compare` | Run both paths and flag disagreements |
| `--doc-id ID` | Scope Atlas searches to one document |
| `--verbose` | Show citations and notes |

**Checkpoint.** The build markers section reports `OK` for every fix, and the
Markdown markers show `PUA bullet chars: 0` and `'Section N' deeper than H1: 0`.

Doing this before embedding catches conversion problems while fixing them is
still free.

---

## Step 8 — Embed and store

**Job:** `tools/embed.py`

```bash
# Always dry-run first
python tools/embed.py build/coc.chunks.jsonl \
    --doc-id acme_ppo_2026 \
    --markdown build/coc.md \
    --tables build/coc.tables.json \
    --dry-run

# Then for real
python tools/embed.py build/coc.chunks.jsonl \
    --doc-id acme_ppo_2026 \
    --markdown build/coc.md \
    --tables build/coc.tables.json
```

| Parameter | Default | Meaning |
|---|---|---|
| `chunks` | required | JSONL from step 5 |
| `--doc-id ID` | from the JSONL | Overrides the document id |
| `--markdown PATH` | none | Stores the Markdown in `coc_documents` — needed to re-chunk later without re-parsing |
| `--tables PATH` | none | Stores the cell-level table structures. Without it the record is saved with `tables_complete: false` and no audit trail |
| `--config PATH` | none | Pipeline YAML |
| `--embed-provider`, `--embed-model`, `--embed-dim` | from env/YAML | Override the embedding configuration |
| `--collection NAME` | `coc_chunks` | Target collection |
| `--dry-run` | off | Report tokens, storage and destination; embed nothing |
| `--no-replace` | off | Keep existing chunks instead of replacing this document's |
| `--resume` | off | Skip chunks already stored — use after an interrupted run |
| `--create-text-index` | off | Also build the lexical index for hybrid search (costs one of three free-tier search indexes) |
| `--recreate-index` | off | Drop and rebuild the vector index. **Required when `embed_dim` changes** |

**Checkpoint.**

```
   vector index 'coc_vector_index': created
   removed 0 existing chunk(s)
   embedded 335 chunk(s) in 41.2s
   stored 335 chunk(s)
   document record stored (18 table structures)
   vector index queryable: True
```

If it stops with a dimension mismatch, that is deliberate: Atlas keeps whatever
`numDimensions` an index was built with, and a query against a mismatched index
returns *nothing* rather than erroring. Re-run with `--recreate-index`.

Re-running is scoped: it deletes only this `doc_id`'s chunks. No collection is
ever dropped.

---

## Step 9 — Verify the index

**Job:** `tools/index_status.py`

```bash
python tools/index_status.py
```

No parameters; reads `.env`.

**Checkpoint.**

```
[READY] coc_vector_index  type=vectorSearch  status=READY
    vector path=embedding dims=1024 similarity=cosine
    filters: doc_id, form_code, needs_review, page_start, plan_id, plan_year, section_path, type
    all expected filter paths present
```

Three things must hold: status `READY` (a building index returns nothing),
`dims` equal to your `EMBED_DIM`, and no missing filter paths — Atlas does not
add filter paths to an existing index retroactively.

Then confirm retrieval end to end:

```bash
python tools/diagnose_denial.py --batch cases.csv --atlas --doc-id acme_ppo_2026
python tools/diagnose_denial.py build/coc.md --batch cases.csv --compare --doc-id acme_ppo_2026
```

`--compare` runs both paths and flags disagreement. A reviewer should never get
a different verdict depending on which source was selected, and the two paths
have diverged twice during development.

---

## Housekeeping

**Rename a document** — preserves embeddings, nothing is re-embedded:

```bash
python tools/rename_doc.py --from OLD_ID --to NEW_ID          # dry run
python tools/rename_doc.py --from OLD_ID --to NEW_ID --apply
```

**Re-run one document** — repeat steps 2–8 with the same `--doc-id`. Step 8
deletes that document's chunks and re-inserts them; other documents are
untouched.

**Change embedding model or dimensions** — repeat steps 5–8, and add
`--recreate-index` in step 8.

**Re-chunk without re-parsing** — the Markdown is in `coc_documents`; pull it
out and resume from step 5.

---

## Quick reference

| # | Job | Writes | Check |
|---|---|---|---|
| 0 | `doctor.py` | — | all `OK` |
| 1 | inspect PDF | — | text layer present |
| 2 | `convert.py` | `.md`, `.tables.json` | headings > 0, tables plausible |
| 3 | `validate.py` | `.validation.json` | `PASS`, 0 blockers |
| 4 | `inventory.py` | — | divisions at H1, ToC matched |
| 5 | `chunk.py` | `.chunks.jsonl` | sizes sane, cost known |
| 6 | `table_audit.py` | — | N of N reassemble |
| 7 | `diagnose_denial.py` | — | known codes found |
| 8 | `embed.py` | Atlas | stored, index queryable |
| 9 | `index_status.py` | — | READY, dims match, filters present |

Steps 0–7 cost nothing and touch nothing. Only step 8 writes to Atlas or spends
money, which is why the checks sit in front of it.
