"""
Unit tests for the FastAPI service.

These test the plumbing, not the answers. Whether Claude's answers are any
good is run_eval.py's job — that one makes real calls, costs real money, and
is run deliberately. These run on every commit, so they must be fast, free,
and offline.

Getting them free requires substituting the two things that reach the network.
Both live on app.state, put there by the lifespan handler at startup, so a
test can assign its own objects there instead. That is the concrete payoff for
storing them on app.state rather than in module-level globals: a global set
inside src.api.app would be reachable only by monkeypatching the module, while
app.state is just an attribute we can overwrite.

Note that TestClient is constructed directly rather than as a context manager
(`with TestClient(app) as c:`). That distinction matters here. Entering the
context manager is what runs the lifespan handler — which would call
connect_to_index() and connect_to_anthropic(), hit the network, and sys.exit()
in CI where no credentials exist. Constructing it plainly skips lifespan
entirely, leaving app.state empty for us to populate with fakes.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.app import app

# A retrieval score comfortably above the 0.30 refusal threshold, and one
# comfortably below it. Named rather than inlined so a future change to
# REFUSAL_THRESHOLD makes the intent of each test obvious.
STRONG_SCORE = 0.55
WEAK_SCORE = 0.05


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
#
# These mimic only the surface the code actually touches — search.format_hit
# reads .id/.score/.fields off a hit, and ask.call_claude reads .content[0].text
# and .usage off a response. Imitating the full Pinecone and Anthropic response
# objects would be wasted effort and would couple the tests to details of those
# libraries that our code never looks at.


class FakeHit:
    def __init__(self, score: float):
        self.id = "bundy-part-01__doc-013::c0"
        self.score = score
        self.fields = {
            "doc_id": "bundy-part-01__doc-013",
            "doc_kind": "teletype",
            "doc_template": "fd-36",
            "source_stem": "bundy-part-01",
            # Stored as strings on purpose: Pinecone metadata rejects list[int],
            # so the real index returns strings here and format_hit converts
            # them back. A fake returning ints would hide that conversion.
            "page_nos": ["21"],
            "case_nums": ["886895"],
            "text": "Bundy escaped from the Pitkin County Courthouse.",
        }


class FakeIndex:
    """Stands in for a Pinecone index.

    Records the last query it received so tests can assert on what the route
    passed down — that is how we verify top_k and doc_kind actually reach
    retrieval rather than being silently dropped.
    """

    def __init__(self, score: float = STRONG_SCORE, hit_count: int = 1):
        self.score = score
        self.hit_count = hit_count
        self.last_query: dict | None = None

    def search(self, namespace, query):
        self.last_query = query
        hits = [FakeHit(self.score) for _ in range(self.hit_count)]
        return type("Resp", (), {"result": type("R", (), {"hits": hits})()})()


class FakeMessages:
    def __init__(self, parent):
        self.parent = parent

    def create(self, **kwargs):
        self.parent.calls.append(kwargs)
        return type(
            "Msg",
            (),
            {
                "content": [type("Block", (), {"text": "A cited answer [doc, p.21]."})()],
                "usage": type("U", (), {"input_tokens": 100, "output_tokens": 20})(),
            },
        )()


class FakeClaude:
    """Stands in for the Anthropic client, recording every call it receives.

    The recording is the point: it lets a test prove Claude was *not* called on
    the refusal path, which is what makes refusal cheap. Asserting only on the
    reported cost would pass even if a call were made and its cost mis-summed.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.messages = FakeMessages(self)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_and_fakes():
    """Yield a TestClient wired to fresh fakes.

    State is reset per test because app is a module-level singleton shared
    across the whole test session; leaving one test's fakes in place would let
    results leak into the next.
    """
    index = FakeIndex()
    claude = FakeClaude()
    app.state.index = index
    app.state.index_name = "casefile-ai-test"
    app.state.claude = claude
    yield TestClient(app), index, claude


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_reports_ok(client_and_fakes):
    client, _, _ = client_and_fakes
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "index": "casefile-ai-test"}


def test_health_does_not_touch_upstreams(client_and_fakes):
    """Health must stay free — platforms poll it constantly."""
    client, index, claude = client_and_fakes
    client.get("/health")
    assert index.last_query is None
    assert claude.calls == []


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------
#
# 422 is FastAPI's code for "your request did not match the declared schema."
# None of these reach the route function, which is the point: the validation
# is generated from the pydantic model, so the handler can assume clean input.


def test_missing_question_is_rejected(client_and_fakes):
    client, _, _ = client_and_fakes
    assert client.post("/ask", json={}).status_code == 422


def test_empty_question_is_rejected(client_and_fakes):
    client, _, _ = client_and_fakes
    assert client.post("/ask", json={"question": ""}).status_code == 422


def test_top_k_above_cap_is_rejected(client_and_fakes):
    """The cap exists because top_k drives prompt size, and over HTTP the
    caller is not the one paying for it."""
    client, _, claude = client_and_fakes
    response = client.post("/ask", json={"question": "x", "top_k": 999})
    assert response.status_code == 422
    assert claude.calls == []  # rejected before any spend


def test_top_k_below_one_is_rejected(client_and_fakes):
    client, _, _ = client_and_fakes
    assert client.post("/ask", json={"question": "x", "top_k": 0}).status_code == 422


def test_oversized_question_is_rejected(client_and_fakes):
    client, _, _ = client_and_fakes
    response = client.post("/ask", json={"question": "x" * 5000})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The answer path
# ---------------------------------------------------------------------------


def test_answer_returns_expected_shape(client_and_fakes):
    client, _, claude = client_and_fakes
    response = client.post("/ask", json={"question": "How did Bundy escape?"})

    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is False
    assert body["answer"] == "A cited answer [doc, p.21]."
    assert body["question"] == "How did Bundy escape?"
    assert len(claude.calls) == 1

    hit = body["hits"][0]
    assert hit["rank"] == 1
    assert hit["doc_id"] == "bundy-part-01__doc-013"
    # Confirms format_hit turned Pinecone's string page numbers back into ints.
    assert hit["page_nos"] == [21]


def test_response_includes_chunk_text(client_and_fakes):
    """The excerpt comes back with the hit.

    This reverses an earlier decision to withhold it. That choice was right
    while the only client was a terminal, where the answer's own quotations
    were enough. The page inspector exists to show a reader the source behind
    each claim, so the text is now the point rather than dead weight, and
    withholding it would only force a second round trip to fetch something
    retrieval already returned.
    """
    client, _, _ = client_and_fakes
    body = client.post("/ask", json={"question": "x"}).json()
    assert body["hits"][0]["text"] == "Bundy escaped from the Pitkin County Courthouse."


def test_usage_includes_cost_estimate(client_and_fakes):
    client, _, _ = client_and_fakes
    usage = client.post("/ask", json={"question": "x"}).json()["usage"]
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["estimated_cost_usd"] > 0


def test_request_parameters_reach_retrieval(client_and_fakes):
    """top_k and doc_kind must actually be passed down, not quietly dropped."""
    client, index, _ = client_and_fakes
    client.post("/ask", json={"question": "x", "top_k": 3, "doc_kind": "newspaper"})
    assert index.last_query["top_k"] == 3
    assert index.last_query["filter"] == {"doc_kind": {"$eq": "newspaper"}}


def test_omitted_doc_kind_sends_no_filter(client_and_fakes):
    """No doc_kind means search everything — not a filter matching nothing."""
    client, index, _ = client_and_fakes
    client.post("/ask", json={"question": "x"})
    assert "filter" not in index.last_query


# ---------------------------------------------------------------------------
# doc_kind validation
# ---------------------------------------------------------------------------
#
# Regression guards for a real trap. doc_kind used to be free text, so any
# unrecognised value filtered the whole corpus away and produced a refusal
# reading "I could not find any documents" — a typo indistinguishable from an
# empty corpus. Swagger's placeholder "string" triggered it in normal use.


@pytest.mark.parametrize("bad_kind", ["string", "newspapers", "Teletype", ""])
def test_unknown_doc_kind_is_rejected(client_and_fakes, bad_kind):
    """Includes the exact placeholder Swagger pre-fills, plus a plural, a
    capitalisation slip, and empty — the realistic ways to get this wrong."""
    client, index, claude = client_and_fakes

    response = client.post("/ask", json={"question": "x", "doc_kind": bad_kind})

    assert response.status_code == 422
    # The failure must happen before retrieval, so no misleading refusal is
    # ever produced and nothing is spent.
    assert index.last_query is None
    assert claude.calls == []


def test_rejection_names_the_valid_kinds(client_and_fakes):
    """The 422 has to be actionable — it should say what is allowed."""
    client, _, _ = client_and_fakes
    detail = client.post("/ask", json={"question": "x", "doc_kind": "string"}).json()
    assert "court-opinion" in str(detail)


@pytest.mark.parametrize(
    "kind",
    [
        "newspaper",
        "court-opinion",
        "loose",
        "form",
        "deletion-sheet",
        "teletype",
        "legal",
        "cover",
        "memo",
    ],
)
def test_every_real_doc_kind_is_accepted(client_and_fakes, kind):
    """The other half of the guard: constraining the field must not lock out a
    kind the corpus actually holds. These nine are the values present in the
    chunk files; if the pipeline gains a tenth, this test and the DocKind
    Literal both need it."""
    client, index, _ = client_and_fakes
    response = client.post("/ask", json={"question": "x", "doc_kind": kind})
    assert response.status_code == 200
    assert index.last_query["filter"] == {"doc_kind": {"$eq": kind}}


# ---------------------------------------------------------------------------
# The refusal path
# ---------------------------------------------------------------------------


def test_weak_match_refuses_without_calling_claude(client_and_fakes):
    """A refusal is a 200: retrieval judged the evidence too weak, which is a
    correct outcome, not a client error. And it must be free."""
    client, index, claude = client_and_fakes
    index.score = WEAK_SCORE

    response = client.post("/ask", json={"question": "What is the capital of France?"})

    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is True
    assert claude.calls == []
    assert body["usage"]["estimated_cost_usd"] == 0.0


def test_refusal_message_reports_the_applied_threshold(client_and_fakes):
    """Regression guard: refusal_message used to print the module constant
    rather than the threshold actually in force, so a custom threshold refused
    correctly but explained itself with the wrong number."""
    client, index, _ = client_and_fakes
    index.score = 0.40  # above the 0.30 default, below the 0.90 we send

    body = client.post("/ask", json={"question": "x", "threshold": 0.9}).json()

    assert body["refused"] is True
    assert "threshold=0.9" in body["answer"]


def test_no_hits_refuses(client_and_fakes):
    client, index, claude = client_and_fakes
    index.hit_count = 0

    body = client.post("/ask", json={"question": "x"}).json()

    assert body["refused"] is True
    assert body["hits"] == []
    assert claude.calls == []


# ---------------------------------------------------------------------------
# Upstream failure
# ---------------------------------------------------------------------------


def test_upstream_failure_returns_502(client_and_fakes):
    """502 rather than 500: we are a gateway and our dependency failed. The
    distinction is what tells you whose code to go read."""
    client, index, _ = client_and_fakes

    def boom(namespace, query):
        raise RuntimeError("pinecone unavailable")

    index.search = boom

    response = client.post("/ask", json={"question": "x"})
    assert response.status_code == 502
    assert "pinecone unavailable" in response.json()["detail"]
