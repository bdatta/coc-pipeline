# COC Pipeline

Turn a health plan Certificate of Coverage (or Summary Plan Description) into a
searchable, citable knowledge base.

A COC is a 200-page PDF whose answers live in benefit tables, and a table cell
means nothing without the column above it and the section above that. This
pipeline converts the PDF to structured Markdown that preserves those
relationships, chunks it without breaking tables, embeds it, and stores it in
MongoDB Atlas — so a question can be answered with the plan's own wording and a
citation back to section and page.

Validated end to end against two real UnitedHealth documents: a 185-page HSA SPD
and a 172-page compilation binding eleven separate form-coded documents.

```
python tools/run_pipeline.py coc.pdf --config pipeline.yaml --embed
```

```
1/6  CONVERT    183 pages, 277 headings, 18 tables (7 spanning page breaks)
2/6  VALIDATE   SCORE 98.2/100   VERDICT: PASS
3/6  INVENTORY  13 named divisions, all at H1
4/6  CHUNK      335 chunks (298 text, 37 table), 121,959 tokens
5/6  AUDIT      18 of 18 tables reassemble cleanly
6/6  EMBED      335 chunks -> coc_rag.coc_chunks
```

---

## Quick start

```bash
git clone <your-repo-url> && cd coc-pipeline
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                 # add MONGODB_URI and VOYAGE_API_KEY
python tools/doctor.py               # verifies deps, credentials, Atlas connectivity

cp pipeline.example.yaml pipeline.yaml
python tools/run_pipeline.py coc.pdf --config pipeline.yaml --embed --dry-run
python tools/run_pipeline.py coc.pdf --config pipeline.yaml --embed
```

`doctor.py` is worth running first on any new machine: it separates missing
packages from missing credentials from an unreachable cluster, so a failure
points at one thing.

For Atlas signup, the database user, IP allow-listing and the connection string,
see [docs/SETUP_ATLAS.md](docs/SETUP_ATLAS.md). The free tier is sufficient — a
185-page document needs about 2.6 MB of the 512 MB allowance.

---

## Configuration

Two files, split along a deliberate line.

**`pipeline.yaml`** — how a document should be processed. Page range, heading
detection, chunk sizes, plan identity, embedding model. Safe to commit next to
the document it describes.

**`.env`** — credentials. `MONGODB_URI`, `VOYAGE_API_KEY`. Gitignored.

The loader **rejects** a YAML file containing anything that looks like a secret
rather than using it silently, so a credential cannot drift into version control
unnoticed. Unknown keys are rejected too: a typo like `tabel_strategy` fails at
load instead of being quietly ignored.

Precedence, lowest first: built-in defaults, environment, YAML, CLI flags.

---

## Commands

| Command | Purpose |
|---|---|
| `tools/doctor.py` | Check dependencies, credentials, Atlas connectivity |
| `tools/run_pipeline.py` | All six steps in one command |
| `tools/convert.py` | PDF → Markdown (+ cell-level tables JSON) |
| `tools/validate.py` | Score and pass/review/quarantine verdict |
| `tools/inventory.py` | Section coverage; `--divisions` lists named sections |
| `tools/chunk.py` | Markdown → chunks JSONL |
| `tools/table_audit.py` | Do a split table's parts reassemble to the whole? |
| `tools/embed.py` | Embed and store in Atlas (resumable) |
| `tools/index_status.py` | Atlas index readiness and filter coverage |
| `tools/diagnose_denial.py` | Trace a provision lookup end to end |

Every step can run standalone:

```bash
python tools/convert.py coc.pdf -o coc.md --tables-json coc.tables.json
python tools/validate.py coc.md --pdf coc.pdf --toc toc.txt
python tools/chunk.py coc.md -o coc.chunks.jsonl --doc-id acme_2026
python tools/table_audit.py --from-jsonl coc.chunks.jsonl --table-id tbl-0002
python tools/embed.py coc.chunks.jsonl --doc-id acme_2026 --markdown coc.md
```

---

## How it works

### Tables survive, including merged cells

A PDF has no concept of a table — only drawing operators. Borders arrive either
as **stroked paths** (`page.lines`) or as **filled rectangles** (`page.rects`);
a 0.5pt-tall filled rect looks identical to a line but is a different operator.
Both are handled: cell edges are clustered into a global grid and each cell
mapped to the rows and columns it spans, which recovers rowspan and colspan
exactly rather than predicting them.

Markdown cannot express merged cells, so two representations are produced: pipe
tables with merged values propagated into every cell they cover (each row stands
alone, which is what retrieval needs), and a cell-level JSON export preserving
true spans for audit.

Tables continuing across a page break are stitched first, with the repeated
header on the continuation page detected and dropped.

### Section context survives chunking

Every chunk carries its heading breadcrumb as metadata *and* prefixed into the
embedded text. A chunk reading only `$40 copay | 50% after deductible` is
unretrievable on its own and uncitable in a letter.

Inside tables the same problem recurs one level down: a row reading
`What Is the Coinsurance You Pay? | 20% | 50%` never mentions acupuncture even
though the banner row above it does, so the running banner is carried onto each
row.

Tables are never split mid-table. An oversized table splits by rows with the
header repeated on each part.

### Bound compilations

A single PDF may bind a medical schedule, a certificate, a drug schedule and
several riders, each restarting at printed page 1 — so "Schedule of Benefits,
p. 12" identifies nothing. Where the footers carry form codes
(`SBN25.CHCSELDP.I.2018.LG.CO 14`), each document is detected and citations
carry the code, staying unambiguous. `validate.py` reports what it found.

Auto-detection of the start page is unreliable on compilations: it finds the
largest consistently numbered block, not the content start. Set `first_page`
explicitly; `convert.py` warns when confidence drops below 90%.

### Validation is a gate, not a report

`validate.py` scores seven checks and returns a verdict — exit `0` pass, `1`
review, `2` quarantine.

Recall carries the largest weight (0.35) because it is the only check the
converter cannot fake: every structural check reads the Markdown and validates
it against the converter's own assumptions, so a systematically wrong
interpretation looks internally consistent. Reconciliation against the source
PDF compares the output to something the converter did not author.

**Embedding is gated on that verdict.** A document below threshold is not
embedded, because doing so costs money and builds an index nobody should trust.
`--force` overrides once the findings have been reviewed.

---

## Re-running

Re-runs are safe and scoped. **No collection is ever dropped.**

| What happens | Scope |
|---|---|
| Default | Deletes chunks for *this* `doc_id` only (`delete_many({doc_id})`), then re-inserts |
| Other documents | Untouched |
| `coc_documents` | The one record for this `doc_id` is replaced |
| Vector index | Reused as-is; never rebuilt unless asked |
| `--resume` | Reads back chunk IDs already stored and embeds only what is missing |
| `--no-replace` | Keeps existing chunks and upserts over them |

Chunk IDs are deterministic and used as `_id`, so re-running upserts in place
rather than duplicating even with replacement turned off.

### Renaming a document

```bash
python tools/rename_doc.py --from OLD_ID --to NEW_ID          # dry run
python tools/rename_doc.py --from OLD_ID --to NEW_ID --apply
```

Moves the chunks and the document record. **Embeddings are preserved** — this
is a rename, not a re-index.

Not a plain `update_many`: a chunk's `_id` is derived from its document
(`acme_2026#00042-a3f9c1d2`) and `_id` is immutable in MongoDB. Updating only
the `doc_id` field would leave every `_id` carrying the old name, and the next
pipeline run — which computes IDs from the *new* name — would insert a second
copy of every chunk instead of upserting over it. The tool reinserts under
freshly computed IDs and removes the originals, and refuses if the target name
already exists.

**The one case that needs care.** Atlas keeps whatever `numDimensions` an index
was created with, and a query against a mismatched index returns *nothing* —
without erroring. So if you change `embed_dim`, switch embedding model, or add a
filter field, the pipeline stops and tells you rather than reusing the old index:

```
!! Vector index 'coc_vector_index' was built for 1024 dimensions;
   this run uses 512. Atlas will not resize it, and queries against a
   mismatched index return nothing rather than failing.
   Re-run with --recreate-index.
```

```bash
python tools/embed.py coc.chunks.jsonl --doc-id acme_2026 --recreate-index
```

Rebuilding takes a minute or two, during which queries return nothing. Check
readiness with `python tools/index_status.py`.

To clear everything and start fresh, drop the collection from the Atlas UI —
that is deliberately not something a CLI flag does.

---

## MongoDB schema

Three collections; [schema/SCHEMA.md](schema/SCHEMA.md) explains each.

| Collection | Purpose |
|---|---|
| `coc_chunks` | One document per retrievable chunk, vector co-located with its text |
| `coc_documents` | One record per COC: Markdown, table structures, settings — re-chunk without re-parsing |
| `coc_query_log` | Optional. Questions and retrieval results — raw material for an evaluation set |

`plan_id` and `plan_year` are vector-index filters, applied inside
`$vectorSearch` rather than after. Answering from the wrong plan year is the
highest-consequence failure this system has.

---

## Tuning a new carrier

Defaults come from two real documents. Two settings are worth checking per
carrier:

| Setting | Default | Change it when |
|---|---|---|
| `treat_bold_as_heading` | `false` | Leave off for documents with a clean font-size hierarchy. On, bold cross-references get promoted — "Section 1: Covered Health Care Services **for details.**" becomes a heading and the tree is wrong from there down. |
| `first_page` | auto-detect | Skip cover and contents. A large cover title claims H1 and prefixes ~130 wasted characters onto every breadcrumb. |

Then verify rather than assume:

```bash
python tools/validate.py coc.md --pdf coc.pdf --toc toc.txt
python tools/inventory.py coc.md --divisions
```

Every named division should sit at **H1**. Anything deeper is flagged — that is
the failure where a subsection replaces its parent on the heading stack and the
section vanishes from every breadcrumb beneath it.

---

### Batch testing / evaluation set

```bash
python tools/diagnose_denial.py coc.md --batch cases.csv
python tools/diagnose_denial.py coc.md --batch cases.csv --verbose
```

```
   CODE     DX       OUTCOME      SCORE  PROVISION
ok S4025    Z31.9    supported    11.95  The following services related to a Gestational...
ok E0240             supported    14.79  Chairs, bath chairs, feeding chairs, toddler...
!! 97810             weak          6.68  Alternative Treatments
8/9 matched the expected outcome.
```

Narrow which sections are searched with `--scopes`:

```bash
python tools/diagnose_denial.py coc.md --batch cases.csv --scopes exclusion,limitation
```

Definitions are searched by default because an exclusion often turns on a
defined term — the exclusion for experimental services does not define
"experimental", the defined-terms section does, and a reviewer needs both. They
are damped (score x0.45) and can never be reported as `supported` on their own,
so they act as context rather than as a basis for denial.

CSV columns — only `code` and `descriptor` are required:

```
code,descriptor,diagnosis_code,diagnosis_descriptor,synonyms,expected
```

`synonyms` is pipe- or semicolon-separated; `expected` is `supported`, `weak` or
`not_found`. Exit code is `1` when any case misses its expected outcome, so this
runs as a regression check.

Run the same cases against Atlas, or against both paths at once:

```bash
python tools/diagnose_denial.py --batch cases.csv --atlas --doc-id acme_2026
python tools/diagnose_denial.py coc.md --batch cases.csv --compare --doc-id acme_2026
```

### Validating against Atlas

`--compare` runs every case through the Markdown and the Atlas path and flags
disagreements. A reviewer should never get a different verdict depending on
which source they selected, and the two paths have diverged twice in practice:

* **Different scoring scales.** The Atlas path scored matched terms unweighted
  while the Markdown path weighted them by rarity — the same provision scored
  1.42 against a threshold of 6.0, so a correct top-ranked hit was reported as
  "no supporting provision found".
* **IDF derived from the result set.** Vector search returns semantically
  similar passages by construction, so a query's own terms are common *within*
  the candidates: searching "bath chair" returns chunks about bath chairs,
  "bath" then looks unremarkable, and the distinctiveness gate rejected the
  right provision. The better the retrieval, the worse the effect. IDF is now
  measured across the document, which is also what the Markdown path does.

Both were invisible from either path alone. `--compare` is the check that
catches this class of problem.

**This file is the evaluation set.** Expansions, reranking, hybrid search and
threshold changes all *feel* like improvements; without a scored case list there
is no way to tell which ones were. A disagreement is not automatically a bug —
it may mean the expected value needs revisiting — but it is always worth
reading. See `examples/denial_cases_example.csv`.

## Known limitations

- **Two-column layouts** are read as a single column.
- **Scanned PDFs** have no text layer. Check with `pdffonts file.pdf`; OCR needs
  an external binary and falls outside the no-runtime-download constraint.
- **Heading detection is heuristic.** Always run `validate.py` before embedding.
- **Table values are unverified.** Structure is checked; whether every extracted
  value matches the source is not. Spot-check several benefit tables against the
  PDF before trusting answers.
- **Scattered small deletions** below roughly 5% are not detected by recall:
  global token accounting credits common words from elsewhere in the document.
  Bulk loss, dropped regions, and mangled tables are all caught.

---

## Requirements

Python 3.10+. All dependencies are pure-Python or wheel-only — nothing downloads
model weights at runtime, which rules out Docling, Marker and
`unstructured[local-inference]`.

MongoDB Atlas free tier is sufficient: 512 MB storage, ~100 operations/second,
three search indexes.
