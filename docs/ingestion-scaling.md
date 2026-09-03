# Ingestion scaling log

Changes made to let the ingestion pipeline handle more than a handful of
documents, in the order they were made, with the measurements that motivated
each one.

The pipeline began as seven stages run one PDF at a time
(`probe → ocr → score → clean → group → chunk → embed`). That is fine for the
three FBI Vault releases it was built on. This log records what breaks beyond
that, and what was done about it.

Each entry follows the same shape: what broke, how it was measured, what
changed, and how the change was proved not to alter output.

---

## 1. Streaming page rendering

*September 2026 · `src/ingestion/ocr.py` · commit `22b8744`*

### The problem

`render_pages()` rendered the entire PDF in one call and returned every page as
a list:

```python
def render_pages(pdf_path, poppler_path):
    return convert_from_path(str(pdf_path), dpi=DPI, poppler_path=poppler_path)
```

`main()` then iterated that list. Every page image therefore existed in memory
simultaneously, and peak memory was the size of the whole rendered document.

A US Letter page at 300 dpi is roughly 2550 × 3300 pixels — about 25 MB as an
uncompressed RGB image. Peak usage scaled linearly with page count, which made
document length a hard ceiling rather than a slowdown. Past a point the run does
not get slower; it fails.

### How it was measured

OCR ran against a **copy** of `bundy-part-02.pdf` named `casefile-bench.pdf`.

This matters. `ocr.py` derives its output directory from the input filename stem
and opens `pages.jsonl` with mode `"w"`. Running it against a real source file
would truncate the enriched `pages.jsonl` that `score`, `clean` and `group` have
since written into — and `data/ocr` is gitignored, so there is no backup to
restore from. Renaming the input redirects all output to a fresh directory and
leaves real data untouched.

Timing came from a stopwatch around the process. Memory was sampled every 400 ms
by polling the running process and tracking its maximum working set.

One trap worth recording: the first two measurement attempts reported a peak of
4 MB, which is impossible for a process rendering page images. The venv's
`python.exe` on Windows is a stub that re-executes the base interpreter as a
child process, so polling the launched process ID measures the launcher rather
than the worker. Matching processes on their command line instead
(`CommandLine LIKE '%ocr.py%'`) produced the real figure.

For a quick check without any of that, Task Manager's Details tab sorted by
memory shows the same climb and plateau.

### Baseline

```
bundy-part-02.pdf — 60 pages @ 300 dpi

wall clock     163.2 s        (2.72 s per page)
  ├─ render     60.7 s
  └─ OCR       101.6 s
peak memory   2,678 MB
```

Extrapolated, with peak memory linear in page count:

| pages | projected peak |
|------:|---------------:|
| 60 | 2.7 GB (measured) |
| 194 (`bundy-part-03`) | ~8.7 GB |
| 1,000 | ~45 GB |

The largest file already in the corpus was close to what a typical laptop can
hold. A thousand-page release would not run at all.

### The change

`render_pages()` became `iter_pages()`, a generator yielding `(page_no, image)`
pairs in batches:

```python
for start in range(1, total + 1, batch_size):
    end = min(start + batch_size - 1, total)
    batch = convert_from_path(
        str(pdf_path), dpi=DPI, poppler_path=poppler_path,
        first_page=start, last_page=end,
    )
    for offset, image in enumerate(batch):
        yield start + offset, image
    del batch
```

Three decisions inside that:

**Batches, not single pages.** Every `convert_from_path` call spawns poppler and
re-reads the PDF structure. Rendering one page per call would pay that cost once
per page — a thousand times for a thousand-page file. Ten pages per call
amortises the overhead while still bounding memory. `RENDER_BATCH = 10` is the
dial: peak is roughly `batch_size × 25 MB`, independent of document length.

**The explicit `del batch`.** Without it the previous batch stays referenced
across the loop boundary while the next is being rendered, so both exist at once
and peak doubles. Dropping the reference at the end of each iteration makes the
old batch collectable before the new one is allocated.

**`count_pages()` is new.** The page count used to come free from
`len(images)`. A generator has no length, so the count is read up front via
`pdfinfo_from_path`, which parses the PDF structure without rendering anything.

### Rejected alternative

Switching the renderer to PyMuPDF was considered and not done. It is already a
dependency (`probe.py` uses it), renders page by page from an open document
handle without spawning a subprocess, and would drop the external poppler
requirement entirely.

It was rejected *for this change* because a different rendering engine produces
different pixels, therefore different OCR text, which makes the byte-identical
verification below impossible. Combining a performance refactor with an
output-changing one would leave no way to tell which caused a regression. It
remains worth evaluating later as a separate, separately measured change.

### Verification

The refactor had to change nothing about extracted text. OCR output feeds every
downstream stage and the eval suite, so a silent difference would shift results
without any test failing.

The pre-change output was kept. The post-change run used a second copy of the
same PDF, writing to its own directory, and the two were compared:

- **All 60 `page_*.txt` files: byte-identical.**
- **`pages.jsonl`: every field matches on every page**, ignoring `source_file`
  and `text_path`, which differ only because the two runs read differently named
  copies of the same document.

Console output *did* change, legitimately. Rendering is no longer a phase that
completes before OCR begins, so there is one timer for the combined pass rather
than separate render and OCR figures. The check is on data files, not stdout.

### Result

| | before | after | |
|---|---:|---:|---|
| Peak memory | 2,678 MB | **356 MB** | −87% |
| Wall clock | 163.2 s | **139.2 s** | −15% |
| Per page | 2.72 s | 2.32 s | |

The memory figure matters less for being 7.5× smaller than for being **flat**.
Peak is now a function of `RENDER_BATCH` rather than document length:

| pages | before | after |
|------:|-------:|------:|
| 60 | 2.7 GB | 356 MB |
| 194 | ~8.7 GB | 356 MB |
| 1,000 | ~45 GB | 356 MB |
| 10,000 | will not run | 356 MB |

**The 15% speedup was not predicted.** The change was expected to be neutral on
time. The likely explanation is that allocating 2.7 GB before any OCR begins
costs more than interleaving the work. It has been measured once, on one file,
so it should be confirmed on a second document before being quoted as a property
of the change rather than an observation about this run.

### What this does not fix

Time is still linear and single-threaded at ~2.3 s/page: 1,000 pages is roughly
40 minutes, 10,000 roughly six and a half hours. OCR is CPU-bound and
embarrassingly parallel — nothing about page 5 depends on page 4 — so this is
addressed by parallelism, not by streaming.

Ingestion also still takes exactly one PDF per invocation, and the `score` stage
still requires a human to choose a confidence threshold per file. Both are fine
for three documents and become the bottleneck at a hundred.

### Reproducing

```powershell
Copy-Item data\raw\bundy-part-02.pdf data\raw\casefile-bench.pdf
Measure-Command { .venv\Scripts\python.exe src\ingestion\ocr.py data\raw\casefile-bench.pdf }
```

Watch `python.exe` in Task Manager's Details tab for the memory figure. Output
lands in `data/ocr/casefile-bench/`, leaving real corpus data alone.

---

## 2. Resumable OCR

*September 2026 · `src/ingestion/ocr.py` · commit `4be8fe5`*

### The problem, plainly

If OCR stopped halfway through a document, it started again from page one.

Imagine photocopying a thousand pages. The copier jams at page 900 — and then
makes you start again from page 1. That was the behaviour. At 2.3 seconds a
page, a thousand-page file takes about forty minutes, so a failure at minute
thirty-eight cost all of it.

It worked that way because the first thing the program did was empty its own
output file and begin afresh. It had no memory of previous runs by design.

### The reason this matters more than it sounds

Resuming after a crash is the obvious benefit. It is not the important one.

The next change parallelises OCR: instead of one worker processing pages 1, 2,
3 in order, several workers each take a page. Eight workers need a checklist on
the wall — without one, two of them photocopy page 40 and nobody photocopies
page 41. And when a worker stops halfway through a page, everyone needs to know
that page is not finished.

The record of what is already done *is* that checklist. So this change is not
really about crash recovery. It is about turning **one page** into a unit of
work that anyone can pick up, and that is safe to redo if it goes wrong.

### What "safe to redo" means

The technical word is *idempotent*: doing something twice gives the same result
as doing it once.

- **"Set the light switch to ON"** — do it twice, still on. Safe to repeat, and
  you never need to know whether someone already did it.
- **"Add one to the counter"** — do it twice and you get 2 instead of 1. You
  must know whether it already happened.

OCR-ing a page is naturally the first kind: *read page 42, save the text to
`page_042.txt`*. Run it a hundred times and the file is the same.

But the old code had the second kind hidden in it. Each page's result was
**appended as a line** to one shared `pages.jsonl`. Run page 42 twice and the
file grows two rows for page 42.

This matters because when work fails, you usually do not know how far it got.
Did it finish page 42 or not? If every piece is safe to repeat, you do not have
to know — you just run it again.

### Three traps

**1. A file existing does not mean it is finished.**

If the process dies *while writing* `page_042.txt`, the file exists but is
truncated. Any "does this file exist?" check says done, and the run carries on
with silently corrupt text. Nothing raises an error.

The photocopier jamming mid-page leaves paper in the output tray. "Is there a
page in the tray?" says yes. Only the top half is printed.

The fix is to not put it in the tray until it is finished: write the text under
a temporary name, then rename it. The operating system guarantees a rename
either happens or does not — never halfway — so the real filename never names a
partial file.

**2. One shared file cannot take many writers.**

Every page appending to a single `pages.jsonl` is a problem twice over: a crash
mid-line leaves an unparseable row, and several workers writing at once have
nothing to serialise on.

So each page now writes its **own** small record, and `pages.jsonl` is assembled
from those records at the end. Workers never touch the same file.

**3. Cached results can go stale.**

Re-render at a different resolution, or replace the source PDF, and the stored
text is wrong but still sitting on disk. Each record therefore notes what it was
made from, and is only reused while that still matches.

### What changed

- Each page writes a `page_NNN.json` sidecar holding its row plus a
  **fingerprint** of the inputs that produced it — a content hash of the source
  PDF plus the DPI. A hash rather than a timestamp because copying a file
  changes its modification time without changing what OCR would produce, and
  file sizes collide too easily. Hashed once per run, not per page.
- Text files and sidecars are written to a temporary name and renamed.
- Text is written **before** its sidecar. A crash between the two leaves an
  orphan text file that the next run simply overwrites. The reverse order would
  leave a sidecar vouching for a page whose text does not exist.
- `pages.jsonl` is assembled from the sidecars once all pages are present.
- A page counts as done only if its sidecar parses, its fingerprint matches, and
  its text file exists. **Any doubt redoes the page** — redoing costs seconds,
  while trusting a bad record corrupts the corpus.
- Render batches containing no unfinished page are never rendered at all, so a
  resumed run skips rendering as well as OCR. Rendering is roughly 40% of the
  per-page cost.
- `--force` ignores all of it and redoes everything.

### Verification

Four checks, on the same 60-page benchmark file:

| Check | Result |
|---|---|
| Fresh run vs. the pre-change output | `pages.jsonl` and all 60 text files byte-identical |
| Running twice | 140.4 s → **0.4 s**, zero pages processed |
| Killed at page 19, then resumed | Output identical to an uninterrupted run; no leftover `.tmp` files; no `pages.jsonl` until the end |
| Sidecar kept, text file deleted | That page alone redone, and rebuilt byte-identical |

The third is the one that matters. It proves an interrupted run and a clean run
produce the same corpus, which is the whole claim.

### Result

```
first run     140.4 s     60 pages processed
second run      0.4 s      0 pages processed
resumed run    100.0 s    41 pages processed  (killed after 19)
```

Wall-clock time for a *first* run is unchanged, as expected — this step does no
less work the first time through. What changed is that work already done is
never repeated, and a page became something that can be handed out, retried, or
picked up by a different worker.

### What this does not fix

Still single-threaded at ~2.3 s/page. Resumability makes parallelism *safe*; it
does not make anything faster on its own. That is the next step.

Ingestion also still takes one PDF per invocation, and `score` still needs a
human to choose a threshold per file.

### Reproducing

```powershell
Copy-Item data\raw\bundy-part-02.pdf data\raw\casefile-bench.pdf
.venv\Scripts\python.exe src\ingestion\ocr.py data\raw\casefile-bench.pdf   # ~140 s
.venv\Scripts\python.exe src\ingestion\ocr.py data\raw\casefile-bench.pdf   # ~0.4 s
```

To see resumption, interrupt the first run with Ctrl-C partway and start it
again — it reports how many pages are already done and continues from there.
