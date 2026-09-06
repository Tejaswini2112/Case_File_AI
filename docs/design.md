# Design

Why this system is built the way it is. The [ingestion engineering
log](ingestion-scaling.md) records what changed over time; this records the
decisions that have held, and what would have to be true to revisit them.

---

## The guarantee everything serves

**An answer must be traceable to a page of a released document, or not given.**

That single constraint decides most of what follows. It is not a general
principle about language models — it is specific to this material. These are
criminal case files. A confident, well-written, plausible answer that is not in
the documents is worse than no answer, because there is no way for a reader to
tell the difference.

The model used here knows a great deal about Ted Bundy. Almost none of it is in
these files. So the system is arranged to make the model's own knowledge
unusable, in two independent places:

- **The prompt** forbids it, and requires an inline `[doc-id, p.N]` citation for
  every claim.
- **The code** refuses to call the model at all when retrieval is weak.

Two mechanisms rather than one because the prompt is a request and the
threshold is a rule. A request can be talked out of; a rule cannot.

---

## Retrieval

### The refusal threshold, and where 0.30 came from

Before answering, the top retrieval score is compared against a threshold. Below
it, the system refuses and no model is called.

The number was measured, not chosen. A diagnostic set of questions was run
against the corpus and the score distributions compared:

```
in-corpus, weakest passing question     0.329
out-of-corpus, best wrong answer        0.240
nonsense questions                      ~0.20
```

0.30 sits in the gap. Above it are questions the corpus can genuinely answer;
below it are questions where the best available match is still the wrong
document.

This is the single most consequential number in the system. Too permissive and
it hallucinates on weak queries; too strict and it refuses legitimate questions
that happen to be phrased oddly. **It is dataset-specific** — the value means
nothing on a different corpus, and it should be re-measured whenever the corpus
changes substantially. Adding a second case is such a change: "out of corpus"
stops meaning "not about Bundy".

A refusal costs nothing, because it happens before the model call. That is not
an optimisation; it is what makes refusing the cheap default rather than a
reluctant one.

### Filtering happens before search, not after

Pinecone applies metadata filters *before* similarity search rather than
discarding results afterwards. Filtering after search wastes work scoring
vectors that were going to be thrown away, and worse, silently shrinks the
result set — ask for five and get two.

### Filters are constrained sets, not free text

`doc_kind` and `case` accept only registered values. This exists because of a
real failure: as free text, a typo filtered the entire corpus away, retrieval
returned nothing, and the system refused with *"I could not find any documents
matching your question"*. Truthful, and completely misleading — it describes a
misspelling as an empty corpus, and the caller cannot tell which happened.

Constrained, a bad value is rejected outright with the valid options named. The
general shape: **prefer failing loudly on impossible input over returning a
plausible-looking empty result.**

---

## Chunking

**~500 tokens, never crossing a document boundary, with ~100 tokens of overlap.**

The document boundary is the important part. A teletype's body must never bleed
into the next newspaper clipping, because a chunk that spans two documents can
be cited as either and belongs to neither.

Splits prefer page boundaries first, then sentences, then words. Overlap exists
so a fact sitting across a cut is still retrievable from either side.

**Page markers travel with the text.** Each page's first sentence is prefixed
`[p.27]`, so the marker ends up inside whichever chunk consumes it. This is what
makes page-level citation possible at all — without it, a chunk spanning pages
27 and 28 could only be cited as "somewhere in this document".

**Withheld pages become a chunk rather than nothing.** A FOIA deletion sheet has
no readable body, so a small synthesised chunk stands in for it. Otherwise
*"what was withheld?"* would be unanswerable, when it is one of the more
interesting questions this corpus can answer.

---

## What each chunk carries, and why

Every field exists because a query needs it. Metadata that no query uses is
weight without purpose.

| Field | Why it exists |
|---|---|
| `chunk_id` | Stable id, so re-indexing overwrites rather than duplicating |
| `doc_id` | Citation target, and what keeps chunks from spanning documents |
| `case` | Scopes retrieval to one investigation |
| `doc_kind` | Filter — "only newspaper coverage", "only court opinions" |
| `page_nos` | Renders the citation a reader can check |
| `case_nums` | FBI file numbers, for looking up a specific file |
| `source_stem` | Which release a document came from |

**Stable ids are the most important of these.** The id is composed from the
document and chunk position, so running ingestion twice overwrites the same
vectors instead of inserting duplicates. Duplicate vectors are the most common
cause of "why is it citing the same thing three times" in RAG systems, and they
are invisible until someone counts.

A consequence worth stating: **document ids must never be renumbered.** They
appear in citations, in chunk ids, and in the index. When documents are merged,
the absorbed ones are removed and the rest keep their numbers, leaving gaps.
Gaps are cosmetic; renumbering would silently repoint every reference.

---

## Cases

**A filter, not separate indexes.**

Separate indexes per case would make cross-case questions — "compare how
evidence was handled in these two investigations" — impossible without querying
several indexes and merging results by hand. A metadata filter keeps that open.

The trade-off is that **an unscoped question searches every case**. That is the
deliberate default, because cross-case comparison is a goal rather than an
accident. The consequence is that scope must be *visible*: the interface shows
which case is being searched, rather than leaving "all cases" as a state
someone is in without knowing.

**Registered cases live in one place** that both ingestion and the API read.
Ingestion rejects an unregistered case at the point of writing; the API rejects
it at the point of asking; and adding a case to that one file gives both. The
alternative — a list in each — is two lists that eventually disagree.

---

## Where human judgement lives

Grouping scanned pages into documents is rules, and rules only reach so far on
damaged 1970s paper. Some documents need a person: a form whose printed header
the detector missed, two pages that are obviously one newspaper article to
anyone who reads them and carry no machine-readable link.

Those judgements are **data in version control**, applied by a pipeline step
that runs every time:

```
corrections/<case>.json
```

Three properties, each from a specific failure:

- **In git, not with the data.** Everything under `data/` is derived and
  regenerates from the sources. This cannot — it records someone reading a
  document. It was previously lost on a rebuild, silently.
- **Applied by the pipeline, not by hand.** These used to be one-off scripts
  run between stages. Re-running the pipeline discarded them without any error,
  and the corpus quietly changed underneath.
- **Every entry explains itself.** A `why` recording what the person saw. In a
  year that sentence is the only way to distinguish a considered correction
  from a mistake, and without it nobody will ever dare remove one.

**A correction naming a document that no longer exists is reported, not
ignored.** It usually means grouping changed and the correction now describes
something that is not there — exactly when a person should look again.

The distinction that matters: a *rule* goes in the grouper, a *judgement* goes
in corrections. Three hand-written scripts were retired by asking that question
of each — two encoded a rule that had been missed, one encoded genuine
judgement.

---

## Ingestion as stages

Eight stages, each a separate program, run in order.

**Separate processes, not function calls.** The boundary is what provides
failure isolation: a corrupt PDF that takes down the interpreter, or a crash
inside the rendering library, kills one file rather than the run. That comes
free from the process boundary rather than from exception handling that has to
anticipate every way a stage can die.

**Work is idempotent at page level.** A page's result is written to a temporary
file and renamed, so the real filename never names a half-written file. This
matters more than crash recovery: it is what makes a page a unit of work that
can be handed to any worker and safely retried, which is what allows the OCR
step to run across processes at all.

**Three outcomes, not two.** A file either ingests, fails, or is *held for
review* — the last when its scan quality does not fit the threshold it was
given. Nothing is broken in that case, but it must not reach the index
unexamined. Folding it into failure would bury it among real errors; folding it
into success would index a document that is mostly discarded pages.

Held files stop before indexing, which is why the check sits mid-pipeline rather
than at the end. Checking afterwards would mean finding bad material already
mixed into the corpus and picking it back out.

---

## The API

**Deliberately thin.** Routes validate a request, call a function that already
existed, and serialise the result. No retrieval logic, no prompt construction,
no thresholds.

The reason is the eval suite. It scores the same function the API calls. If the
API reimplemented any part of the loop — even something as small as its own
default `top_k` — the thing being served and the thing being measured would
drift apart, and the eval would quietly stop telling the truth about
production. **One code path, two front ends.**

Two consequences of being reachable over HTTP rather than a terminal:

- **Inputs need ceilings.** `top_k` drives prompt size, and on the command line
  the person typing is the person paying. Over HTTP that stops being true.
- **Nothing may exit the process.** The connection helpers exit on missing
  credentials, which is right for a script and fatal in a request handler. They
  run once at startup instead, so a misconfigured deployment refuses to boot
  rather than dying on someone's first question.

---

## Testing

Two suites doing different jobs.

**Unit tests** (43, ~2s, free) test plumbing. Both network dependencies are
replaced with stand-ins, so they run with no credentials and no cost — which is
what lets them run on every push. They check that bad input is rejected, that
filters reach retrieval, that a refusal does not call the model.

That last one is asserted by checking the fake model was never called, rather
than by checking the reported cost is zero. A cost of zero would also be
reported if a call happened and the arithmetic was wrong.

**The eval suite** (17 questions, real calls, real money) tests answers. It is
run deliberately, not on every push, because it costs money and is
non-deterministic.

**Failures in the eval are signal, not bugs.** Two questions fail today and are
documented rather than fixed, because the honest fix is a design change rather
than a threshold nudge.

---

## What is deliberately not built

Each of these would be reasonable. None is warranted yet.

- **Conversation state.** Every question is independent. Scope inferred from
  chat history was considered and deferred — it needs conversation state to
  exist first, and inferred scope must be visible or it becomes another silent
  decision.
- **Automatic threshold selection.** The system flags when the threshold you
  gave looks wrong for a document; it does not pick a better one. Flagging is
  more honest than guessing.
- **Per-word OCR confidence.** Tesseract returns it. It is averaged per page and
  the rest discarded, so misread words cannot be highlighted without re-running
  OCR. Worth doing when there is an interface that would show it.
- **Overlapping pipeline stages.** While one file is being scored and cleaned,
  the next could be OCR'ing. It is the largest remaining throughput win and it
  deserves its own measurement rather than being assumed.
- **A storage abstraction.** One module knows where data lives and a single
  setting relocates it. That is a place for the layout to live, not a layer.
  Object storage should be added when something actually needs it.

The common thread: **build the thing that a specific observed problem asks for.**
Every entry in the engineering log begins with a measurement or a failure, not
with an intention.
