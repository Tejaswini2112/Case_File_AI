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

---

## 3. Using more than one processor

*September 2026 · `src/ingestion/ocr.py` · commit `2cf8b76`*

### The problem, plainly

This machine has twelve processors. OCR used one. The other eleven sat idle
for the entire run.

Picture twelve photocopiers in a room, and one person feeding pages into a
single machine while the other eleven stay switched off.

The work is a good fit for spreading out, because **no page needs any other
page**. Page 5 does not depend on page 4 in any way. They only happened one
after another because that is how the program was written, not because they
have to.

### What it actually gained

```
1 worker     134.6 s
8 workers     56.5 s      2.4x faster
```

**This is less than expected.** The estimate beforehand was four to six times
faster. It came out at 2.4. Recorded here rather than quietly rounded up,
because the gap between the guess and the measurement is the useful part.

### The surprise worth knowing about

Running more copies does not keep making it faster. Past a point it makes it
slower:

| workers | time | versus one worker |
|--------:|-----:|------------------:|
| 1 | 188.8 s | — |
| 2 | 106.7 s | 1.8x |
| 4 | 68.1 s | 2.8x |
| 8 | **51.6 s** | **3.7x** ← best |
| 12 | 74.4 s | 2.5x ← slower than 4 |

(These were taken while also measuring memory, which slows everything down a
little. The shape is what matters; the honest times are the two above.)

**Twelve is worse than four.** The machine reports twelve processors, but that
is really about six, each pretending to be two. Push past what is actually
there and the copies spend their time queueing for a turn instead of working.

The obvious setting — "use every processor" — would have made ingestion slower
than necessary, and nothing would have revealed it. This is the entire argument
for measuring rather than assuming. The default is now half the reported
processors, which sits safely on the good part of the curve, and `--workers`
overrides it.

### Two things that would have spoiled it

**Memory comes back.** The earlier streaming change got one run down to
356 MB by holding only ten pages at a time. But that was one worker. Twelve
workers each holding ten pages would need around 3 GB — undoing the earlier
work entirely.

Each photocopier needs its own desk space for the stack it is working through.
Twelve machines means twelve stacks in the room.

So the stack each worker holds shrinks as the number of workers grows.

**Everyone hires assistants.** This is the one that catches people out. The OCR
software already tries to use several processors by itself. So starting twelve
copies means each one quietly spins up its own helpers, and suddenly forty-odd
things are competing for twelve processors. They spend longer taking turns than
working, and the parallel version can finish *slower* than the single one.

Twelve people in a room that fits twelve, each hiring three assistants.
Everybody is elbowing everybody.

The fix is to tell each copy to use exactly one processor, and let this program
be the only thing deciding how much to run at once. One decision-maker, not two.

### Why the results stay correct

Workers finish in whatever order they happen to finish. Page 40 may be done
before page 12.

Normally that is a problem — results arriving out of order. Here it is not,
because of the previous change: `pages.jsonl` is built from the per-page records
at the end, **sorted by page number**. Which worker finished first makes no
difference to what comes out.

That is the payoff for making pages independent before trying to run them in
parallel. This step needed almost no new coordination, because the previous one
had already removed the need for it.

### Verification

| Check | Result |
|---|---|
| 8-worker output vs single-worker output | byte-identical — `pages.jsonl` and all 60 text files |
| Eight workers killed mid-run | clear message, no crash dump, no half-written files, 20 finished pages kept |
| Resuming that killed run | output identical to a clean single-worker run |

The first is the important one. Spreading work across processors must not change
a single character of the result, and it does not.

### Two smaller fixes

Killing the workers used to print a wall of red error text. It now prints one
sentence saying what happened and that finished pages are safe. It also
deliberately does **not** write `pages.jsonl` when a run stops early — a partial
one would look complete to every later stage, which is worse than having none.

Error messages are now printed in the same character encoding as normal output.
A dash in one message had been coming out as garbled characters on Windows.

### What this does not fix

Ingestion still handles one PDF per command, and the scoring stage still needs a
person to pick a quality threshold for each file. Fine for three documents;
the bottleneck at a hundred.

### Reproducing

```powershell
Copy-Item data\raw\bundy-part-02.pdf data\raw\casefile-bench.pdf
.venv\Scripts\python.exe src\ingestion\ocr.py data\raw\casefile-bench.pdf --force --workers 1
.venv\Scripts\python.exe src\ingestion\ocr.py data\raw\casefile-bench.pdf --force --workers 8
```

Times vary by roughly ten percent between runs, so a single pair of numbers is
an observation rather than a fact. The shape of the curve held across every
attempt.

---

## 4. Ingesting a folder instead of a file

*September 2026 · `src/ingestion/batch.py` · commit `b8803fb`*

### The problem, plainly

Ingesting a document was one command. Ingesting a hundred documents was a
hundred commands, typed one at a time, each waiting for the last to finish.

Worse, the first failure ended everything. Start a run before going to bed,
and if the fourth file is corrupt you come back to three files done and
ninety-six never attempted.

### Why this is not just a loop

A loop is one line of shell. The reason it needed to be a real program is what
happens when things go wrong — and across a hundred files, something always
does.

**A broken file must not stop the rest.** Each document now runs in its own
separate process. If one crashes outright, it takes itself down and nothing
else. That isolation comes from the separation itself rather than from trying
to predict every way a document can fail.

**You need to know what happened.** After two hours you want a summary, not
thousands of lines to scroll back through. Each document's detailed output goes
to its own log file, and the screen shows one line per document.

**It should skip what is already done.** Drop ten new files into the folder,
re-run, and it processes ten — not all hundred and ten.

**Biggest first.** If the longest document runs last, everything else finishes
and the batch sits waiting on it alone. Starting it first overlaps it with the
short ones. This costs nothing — it is a sort order.

**Say what it will do before doing it.** A dry run prints "12 files, 3,204
pages, roughly 12 minutes" so a mistake is caught before an afternoon is spent
on it.

### What a run looks like

```
[1/3] alpha.pdf      4p  ok in 11s
[2/3] gamma.pdf      3p  ok in 18s
[3/3] beta.pdf       2p  FAILED — see data/ocr/beta/ingest.log

Done in 46s.
  ingested  : 2
  skipped   : 0
  failed    : 1
```

### Two bugs found while testing, both the same shape

Both were **a failing run reporting success**, which is the worst kind of bug
in something you leave running overnight.

A corrupt file is spotted early, when the program reads how many pages each
document has. Because it never became a job, it never appeared in the failure
count — so a folder where half the files were corrupt would have reported zero
failures.

The second was worse. Once everything else was finished, the program returned
early and never reached the part that reports failures at all. A nightly run
whose only remaining problem was one permanently broken file would have
reported clean **forever**.

Both now count as failures, and the run reports failure to whatever started it.

### Verification

| Check | Result |
|---|---|
| Dry run | matched what the real run did, biggest file first |
| One corrupt file among three good ones | corrupt one named, other three completed |
| Re-running | all three finished files skipped |
| Every file forced to fail partway | ran through all of them, recorded each with its log |
| Reported success or failure correctly | yes, including when the only problem left was a corrupt file |

### One thing this needed

The pipeline's final stage writes to the live search index. Testing the batch
driver would therefore have pushed duplicate test documents into the real
corpus — the same corpus the quality evaluation is scored against.

So a way to stop before that stage was added. It is useful beyond testing:
sometimes you want to prepare documents without publishing them.

### What this does not fix

Every file still got the same quality threshold, and that threshold still had
to be chosen by a person. Making it compulsory forced the decision rather than
making it. That is the next entry.

---

## 5. Deciding quality without a person

*September 2026 · `src/ingestion/score_pages.py` · commit `7d931df`*

### The problem, plainly

One stage needed a human. After reading a document, the program showed how
clearly each page had scanned, and someone had to look at that spread and
choose a cut-off: below this, a page is too garbled to be worth keeping.

Sensible for three documents. Impossible for a hundred.

### Does one cut-off work for every file?

The obvious worry is that different documents need different cut-offs. For this
corpus, they do not:

| file | pages | discarded | typical page |
|---|---:|---:|---:|
| bundy-part-01 | 85 | 24% | 76.8 |
| bundy-part-02 | 60 | 25% | 72.0 |
| bundy-part-03 | 194 | 20% | 80.0 |

Nearly identical. But notice **why**: all three are releases of the same case,
scanned by the same people on the same equipment in the same era. One cut-off
works because they share a history, not because one number is universally
right.

It would stop working for a batch scanned on better equipment, or for modern
documents mixed in with old scans. And there is a genuinely dangerous case: if
a document's typical page scores below the cut-off, nearly all of it is thrown
away — and nothing says so. The file "succeeds" with almost nothing in it.

### Why not a different cut-off per file

Tempting, and wrong.

The cut-off is an **absolute** standard: text this garbled is not worth keeping,
regardless of what else is in the document. Making it relative — *keep the best
80% of each file* — would rank pages instead of judging them. A uniformly
terrible scan would keep its least-terrible pages, still unreadable. A pristine
one would throw away good pages for no reason.

So the standard stays fixed. What varies is whether we **trust** it for a
particular document.

### What changed

Two checks now run automatically, neither of which needs to know anything about
other files:

- more than **half** the pages discarded
- the **typical** page falling below the cut-off

The 50% figure comes from the table above. A fifth is normal for this corpus, so
half means something is genuinely different about the document rather than
slightly rougher.

### A third answer was needed

Until now a file either worked or failed. A document whose scan does not fit the
cut-off is neither — nothing is broken, but it must not be added to the search
index without someone looking.

So there are three outcomes:

```
ingested     : 47
skipped      :  3   (already done)
needs review :  2
    release-14.pdf  —  71% of pages below the cut-off
    release-22.pdf  —  typical page scores 48.3, below the cut-off
failed       :  1
```

**The check happens before anything is published, not after.** A held document
stops at the scoring stage, so nothing about it reaches the search index. That
is deliberate: checking afterwards would mean finding the bad material already
mixed into the corpus and having to pick it back out.

The human judgement has not been removed. It is now only asked for on the
documents where the automatic answer cannot be trusted.

### The override, and its one limit

A held file can be forced through, for when you have looked and decided it is
fine. The check is a speed bump, not a wall.

But it cannot force through a file where **every** page was discarded. That is
not a matter of judgement — there is simply nothing to add.

This was found by testing the override. Forcing such a file through made it fail
three stages later with a confusing message about missing fields, nowhere near
the real cause. It now says plainly that there is nothing to ingest.

### Verification

| Check | Result |
|---|---|
| Normal cut-off | both test files ingested, checks silent |
| Deliberately impossible cut-off | both held, with reasons given, nothing published |
| Override on a merely poor file | ingested |
| Override on a file with nothing usable | still held, with a message naming the real problem |
| Existing corpus | all three real files pass, checked without altering them |

### What this does not fix

It does not choose a better cut-off for you. It uses the one you give and tells
you when that number looks wrong for a particular document. Choosing
automatically is a larger question, and flagging is more honest than guessing.
