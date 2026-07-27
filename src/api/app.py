"""
Phase 2 — HTTP service exposing the RAG loop.

This is deliberately a thin layer. Every route does three things and nothing
more: validate the request, call a function that already existed, serialize the
result. No retrieval logic, no prompt construction, no thresholds live here.

That constraint matters more than it looks. The eval suite (tests/run_eval.py)
scores `answer_question()`. If the API reimplemented any part of the loop —
even something as small as its own default top_k — the thing being served and
the thing being measured would drift apart, and the eval would quietly stop
telling the truth about production. One code path, two front ends.

Run it:
    .venv/Scripts/python.exe -m uvicorn src.api.app:app --reload

Then:
    http://127.0.0.1:8000/docs     interactive API explorer (generated, free)
    http://127.0.0.1:8000/health   readiness check
"""

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from src.agents.ask import (
    DEFAULT_MODEL,
    DEFAULT_TOP_K,
    REFUSAL_THRESHOLD,
    answer_question,
    connect_to_anthropic,
    connect_to_index,
)

# Bound the blast radius of a hostile or careless caller. top_k feeds straight
# into the prompt, so a request for 500 chunks is a request for a very large
# and very expensive Claude call on our account. The CLI has no such cap
# because the person typing the command is the person paying for it; over HTTP
# that stops being true.
MAX_TOP_K = 20
MAX_QUESTION_CHARS = 1000

# The doc_kind values the corpus actually contains, taken from the chunk files
# the ingestion pipeline produces.
#
# Declaring them closes a trap. doc_kind is a pre-filter: Pinecone applies it
# before searching, so an unrecognised value matches no chunks and retrieval
# returns empty. answer_question() then refuses, truthfully but misleadingly —
# "I could not find any documents matching your question" describes a typo as
# an empty corpus, and the caller has no way to tell those apart. As a free
# text field this was easy to hit: Swagger's "Try it out" pre-fills every
# string box with the placeholder "string", which silently filtered away all
# 371 chunks.
#
# Constrained, a bad kind is a 422 that lists the valid ones, and the generated
# docs render a dropdown instead of a text box, so the placeholder never
# appears. The general shape: prefer failing loudly on impossible input over
# returning a plausible-looking empty result.
#
# This list is coupled to the pipeline — a new doc_kind in chunk_documents.py
# or chunk_opinions.py has to be added here too, or the API will reject a kind
# the corpus really holds. It is not derived from the data at import time on
# purpose: data/ocr is gitignored, so nothing here can read the corpus in CI.
DocKind = Literal[
    "newspaper",
    "court-opinion",
    "loose",
    "form",
    "deletion-sheet",
    "teletype",
    "legal",
    "cover",
    "memo",
]


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Open connections once, before the first request, and reuse them for the
    life of the process.

    Everything before `yield` runs at startup; everything after runs at
    shutdown. We have nothing to clean up — the Pinecone and Anthropic clients
    hold pooled HTTP connections that die with the process — so the second half
    is empty. It stays as a marker for where cleanup goes when something here
    needs closing.

    Two reasons this is not per-request work:

      - connect_to_index() makes a live describe_index() call to resolve the
        index host. Per request, that is a network round-trip added to every
        query before any real work starts.
      - Both connect helpers call sys.exit() when credentials are missing.
        Inside a request handler that would take down the whole server. Here it
        happens during startup, so a misconfigured deployment fails immediately
        and visibly instead of serving traffic that dies on first use.

    Storing them on `app.state` is FastAPI's idiom for process-wide singletons.
    The alternative — module-level globals — works but makes the objects
    invisible to tests, which then cannot substitute fakes for them.
    """
    index, index_name = connect_to_index()
    app.state.index = index
    app.state.index_name = index_name
    app.state.claude = connect_to_anthropic()
    yield


app = FastAPI(
    title="Case File AI",
    description="Ask questions about declassified FBI case files and get cited answers.",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------
#
# These pydantic models are not documentation. FastAPI reads them at import
# time and generates real behavior from them: request bodies are parsed and
# type-checked before the route function runs, bad input is rejected with a 422
# and a field-level error message we never wrote, and /docs is built from the
# same definitions. Declaring the shape *is* implementing the validation.


class AskRequest(BaseModel):
    question: str = Field(
        ...,  # Ellipsis means required — no default, omitting it is a 422.
        min_length=1,
        max_length=MAX_QUESTION_CHARS,
        description="The question to ask the corpus.",
    )
    top_k: int = Field(
        DEFAULT_TOP_K,
        ge=1,
        le=MAX_TOP_K,
        description="How many chunks to retrieve before answering.",
    )
    threshold: float = Field(
        REFUSAL_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="Refuse to answer if the top hit scores below this.",
    )
    model: str = Field(DEFAULT_MODEL, description="Anthropic model id.")
    doc_kind: DocKind | None = Field(
        None,
        description="Restrict retrieval to one document kind. Omit to search everything.",
    )


class Hit(BaseModel):
    """One retrieved chunk, as returned to the client.

    Note what is absent: `text`. The retrieved chunk bodies are in the model's
    context, not in the response. They are large, they are OCR noise, and the
    answer already quotes what matters. Callers get enough to verify a citation
    — which document, which pages, how strong the match — without shipping
    kilobytes of raw scan text over the wire on every request.
    """

    rank: int
    chunk_id: str
    doc_id: str | None
    doc_kind: str | None
    page_nos: list[int]
    case_nums: list[str]
    score: float


class AskResponse(BaseModel):
    question: str
    answer: str
    refused: bool
    model: str
    hits: list[Hit]
    usage: dict


class HealthResponse(BaseModel):
    status: str
    index: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """
    Liveness/readiness probe. Deliberately cheap: it reports that startup
    completed and connections exist, and does not query Pinecone or Claude.

    A health check that does real work is a health check that costs money and
    fails for reasons unrelated to our health — a slow upstream would mark us
    down while we are perfectly able to serve. Orchestrators poll this
    endpoint constantly; it must stay nearly free.
    """
    return HealthResponse(status="ok", index=request.app.state.index_name)


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, request: Request) -> AskResponse:
    """
    The one endpoint that matters. Retrieve, decide, generate — all of it
    delegated to answer_question().

    Note this is `def`, not `async def`, and that is load-bearing. answer_question()
    is synchronous and spends nearly all its time blocked on network I/O to
    Pinecone and Anthropic. In an `async def` route, a blocking call occupies
    the event loop thread, and since that single thread drives every connection
    the server has, one in-flight question would stall every other request
    until Claude replied. Declaring the route as plain `def` makes FastAPI run
    it in a worker threadpool instead, so slow requests block a thread rather
    than the whole process. Making this route `async` without also making the
    Anthropic and Pinecone calls async would be a serious and near-invisible
    performance bug — it looks more modern and behaves far worse.

    A refusal is a 200, not a 4xx. The service worked exactly as designed: it
    searched, judged the evidence too weak, and said so. That is a successful
    answer to a hard question, and `refused: true` carries the distinction
    without lying about the HTTP layer.
    """
    try:
        result = answer_question(
            request.app.state.index,
            request.app.state.claude,
            question=req.question,
            top_k=req.top_k,
            threshold=req.threshold,
            model=req.model,
            doc_kind=req.doc_kind,
        )
    except Exception as exc:
        # Pinecone or Anthropic failed — upstream outage, rate limit, bad model
        # id. 502 is the honest code: we are a gateway and our dependency
        # failed. The exception text goes in the detail because this is a
        # portfolio project where a readable error beats a hidden one; a service
        # handling real user data would log the detail and return something
        # generic, since exception messages leak internals to callers.
        raise HTTPException(status_code=502, detail=f"Upstream failure: {exc}") from exc

    return AskResponse(
        question=result["question"],
        answer=result["answer"],
        refused=result["refused"],
        model=result["model"],
        hits=[
            Hit(
                rank=i,
                chunk_id=h["chunk_id"],
                doc_id=h["doc_id"],
                doc_kind=h["doc_kind"],
                page_nos=h["page_nos"],
                case_nums=h["case_nums"],
                score=h["score"],
            )
            for i, h in enumerate(result["hits"], 1)
        ],
        usage={**result["usage"], "estimated_cost_usd": round(result["cost_usd"], 6)},
    )
