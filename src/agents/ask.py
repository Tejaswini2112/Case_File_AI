"""
Step 8 — Ask a question, get a cited answer from Claude grounded in the corpus.

The full RAG loop. Three responsibilities, in order:

  1. Retrieve: use search.py to pull the top-k most relevant chunks from
     Pinecone. (We import the existing function rather than re-implementing —
     never re-validate what's already proven.)

  2. Decide: if the top-1 score is below REFUSAL_THRESHOLD, refuse to answer
     and tell the user the corpus doesn't contain this. This is the single
     most important production guardrail in any RAG system — it prevents
     the LLM from hallucinating confident-sounding garbage when given weak
     context. Threshold was calibrated empirically in step 7's diagnostic
     suite (in-corpus floor ~0.40 vs out-of-corpus ceiling ~0.24).

  3. Generate: hand the chunks + question to Claude with a strict citation
     prompt. Claude must cite every claim using [doc-id, p.N] inline. We
     refuse general-knowledge supplementation — even if Claude "knows"
     Bundy was active in Florida, it cannot say so unless the excerpts
     contain that fact.

Usage:
    python src/ask.py "What evidence was used against Bundy?"
    python src/ask.py "..." --top-k 8
    python src/ask.py "..." --model claude-opus-4-7
    python src/ask.py "..." --json    # structured output for eval pipeline
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

# Reuse the validated search function from the retrieval package. Importing it
# (rather than copying the logic here) keeps a single source of truth for
# retrieval. Putting the repo root on sys.path makes the absolute import work
# when ask.py is invoked directly as `python src/agents/ask.py` (cwd = root).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.retrieval.search import connect_to_index, search

sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Sonnet 4.6 is the Phase 1 default. Reasoning:
#   - Strong enough to reason over noisy OCR text and produce careful citations
#   - ~5x cheaper per token than Opus, ~3x more expensive than Haiku
#   - Haiku would tempt with the price but struggles with citation discipline
#     on this corpus (citing chunks that don't actually support the claim)
#   - Opus is overkill for Phase 1; revisit if hard cases emerge in eval
DEFAULT_MODEL = "claude-sonnet-4-6"

# Same default as search.py — k=5 is the conventional RAG starting point.
# In step 9 (eval) we'll measure whether 3 or 8 is better for this corpus.
DEFAULT_TOP_K = 5

# Refusal threshold, calibrated from step 7's diagnostic suite:
#
#   In-corpus  (Q1 floor)     0.459
#   In-corpus  (Q11c floor)   0.329  ← weakest passing semantic match
#   Out-corpus (Q6 ceiling)   0.240
#   Nonsense   (Q7 ceiling)   ~0.20
#
# 0.30 splits the difference: Q11c (pure conceptual paraphrase, real match)
# still passes; Q6 (out-of-corpus best-wrong-answer) gets refused.
#
# Threshold is the SINGLE most impactful tunable in production RAG. Too
# permissive = hallucinations on weak queries. Too strict = false refusals
# on legitimate questions phrased oddly. Calibrate via the eval set.
REFUSAL_THRESHOLD = 0.30

# Generation knobs.
# - temperature=0 for factual Q&A: deterministic, no creative liberties
# - max_tokens=1024 caps essay-length drift; we want focused answers
MAX_TOKENS = 1024
TEMPERATURE = 0.0

# Approximate per-million-token prices (USD) for cost printing. These are
# rough — Anthropic adjusts pricing periodically. The display is informational,
# not billable; refresh from the pricing page if you want exact numbers.
PRICING_PER_MTOK = {
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-7":            {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
}


# ---------------------------------------------------------------------------
# The system prompt — the contract Claude operates under
# ---------------------------------------------------------------------------
#
# This is the most consequential text in the file. Production teams version
# their prompts in source control and A/B test changes — for now we keep it
# inline as a constant. Phase 3 typically moves prompts to LangFuse or a
# dedicated prompts/ directory.
#
# Structural elements (general production pattern):
#   1. ROLE — who Claude is
#   2. INPUT FORMAT — what Claude will see
#   3. CITATION RULES — how Claude must attribute claims
#   4. GROUNDING RULES — when Claude must refuse / what Claude can't say
#   5. STYLE RULES — what the output should look like
#
# Anti-hallucination is enforced in TWO places — the grounding rules AND the
# code-side refusal threshold. Belt and suspenders: the prompt asks Claude
# not to hallucinate, the threshold prevents Claude from being given the
# context that would tempt it.

SYSTEM_PROMPT = """You are a research assistant analyzing declassified FBI \
case files. You answer questions using ONLY the document excerpts provided \
in the user message — never use your general knowledge.

INPUT FORMAT
You will receive a question and a numbered list of document excerpts. Each \
excerpt is labeled with its document ID, page numbers, and document kind \
(teletype, newspaper, form, legal, cover, deletion-sheet). Page markers like \
[p.27] appear inline within excerpts to anchor specific facts to specific pages.

CITATION RULES
- Every factual claim MUST cite a source from the excerpts.
- Use the format [doc-id, p.N] inline directly after the claim.
  Example: "Bundy escaped from the Pitkin County Courthouse [bundy-part-01__doc-013, p.21]."
- If multiple excerpts support a claim, cite all relevant: \
[bundy-part-01__doc-013, p.21; bundy-part-01__doc-015, p.23].
- Short direct quotes are encouraged when they sharpen the point.
- Never cite a doc-id that isn't in the provided excerpts.

GROUNDING RULES
- If the excerpts do not contain the answer, say so explicitly: \
"The provided documents do not contain information about [topic]."
- Do NOT supplement with general knowledge. Even if you "know" something about \
Ted Bundy from training data — for example his Florida activity or eventual \
execution — do not state it unless a provided excerpt supports it.
- These documents are OCR'd from typewritten 1970s FBI forms. Text may contain \
garbled words, redacted spans marked [REDACTED], or stray symbols. Interpret \
the text charitably but quote it carefully — preserve oddities when quoting.

STYLE RULES
- Lead with a direct 1-3 sentence answer.
- Follow with supporting details + citations.
- If the question has multiple parts, address each separately.
- No section headers or markdown unless the answer is genuinely list-shaped \
(multiple charges, multiple dates, multiple sources of evidence).
- Be concise. The user reads the excerpts too; you don't need to restate them.
"""


# ---------------------------------------------------------------------------
# Refusal — the production guardrail
# ---------------------------------------------------------------------------


def should_refuse(hits: list[dict], threshold: float) -> bool:
    """
    Return True if retrieval was too weak to trust answering.

    Why check top-1 (not the average): a single strong hit is enough material
    for a defensible answer; the rest of top-k is supporting context. If
    even the strongest hit is below threshold, the corpus genuinely doesn't
    contain the answer and Claude would have to invent one.
    """
    if not hits:
        return True
    return hits[0]["score"] < threshold


def refusal_message(question: str, hits: list[dict]) -> str:
    """
    User-facing refusal text. Honest about what we looked at and why we
    declined — better UX than a bare "no results."
    """
    if not hits:
        return (
            f"I could not find any documents matching your question:\n"
            f"  {question!r}\n"
        )
    top = hits[0]
    return (
        f"The documents I searched do not contain a confident answer to your "
        f"question:\n"
        f"  {question!r}\n\n"
        f"The closest match (score={top['score']:.3f}, threshold={REFUSAL_THRESHOLD}) "
        f"was {top['doc_id']} on page(s) {top['page_nos']}, but it is not strong "
        f"enough to answer from. Try rephrasing, or run search.py to see what is "
        f"available in the corpus."
    )


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def format_excerpt(hit: dict, index: int) -> str:
    """
    Render one retrieved chunk for Claude. The label line is engineered so
    Claude can cite directly using the same string format we ask for in the
    system prompt.
    """
    pages = ", ".join(str(p) for p in hit["page_nos"])
    case_str = ", ".join(hit["case_nums"]) if hit["case_nums"] else "—"
    return (
        f"[{index}] {hit['doc_id']} | pages {pages} | kind={hit['doc_kind']} "
        f"| case#={case_str} | retrieval_score={hit['score']:.3f}\n"
        f"{hit['text']}"
    )


def build_user_message(question: str, hits: list[dict]) -> str:
    """
    Assemble the user-side prompt. Order matters:
      1. Excerpts first — Claude reads context with fresh attention.
      2. Question last — recency means the model treats the question as the
         most salient instruction.
      3. Final reminder of the citation contract.

    This "context-then-question" pattern outperforms "question-then-context"
    on long-context tasks. The reason is the "lost in the middle" attention
    pattern — content at the start and end of context gets more attention
    weight than content in the middle. Putting the question last guarantees
    Claude attends to it strongly when generating.
    """
    excerpts = "\n\n".join(format_excerpt(h, i) for i, h in enumerate(hits, 1))
    return (
        f"DOCUMENT EXCERPTS:\n\n{excerpts}\n\n"
        f"---\n\n"
        f"QUESTION: {question}\n\n"
        f"Answer using ONLY the excerpts above. Cite every claim inline using "
        f"the [doc-id, p.N] format. If the excerpts don't contain the answer, "
        f"say so."
    )


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------


def connect_to_anthropic() -> Anthropic:
    """Create the Anthropic client. Fail loudly if the key is missing."""
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit(
            "ANTHROPIC_API_KEY is not set in .env.\n"
            "Get one from https://console.anthropic.com -> Settings -> API Keys."
        )
    return Anthropic(api_key=api_key)


def call_claude(
    client: Anthropic,
    user_message: str,
    model: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, dict]:
    """
    Single Claude call. Returns (answer_text, usage_dict).

    Why not stream: streaming improves perceived latency in a UI but adds
    complexity (SSE handling, partial-token assembly) that buys nothing for
    a CLI script. Phase 2 with a real UI will switch to streaming.

    Why temperature=0: factual Q&A — we want determinism. The same question
    over the same excerpts should produce the same answer on every run. This
    also makes Step 9's eval set reproducible.
    """
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    # response.content is a list of content blocks. For text-only responses
    # there's exactly one block. Tool-use responses would have multiple —
    # not relevant until Phase 2 when we add tool calls.
    answer = response.content[0].text
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return answer, usage


def estimate_cost_usd(usage: dict, model: str) -> float:
    """Cheap cost estimate. Useful as a habit; not authoritative."""
    pricing = PRICING_PER_MTOK.get(model)
    if not pricing:
        return 0.0
    return (
        usage["input_tokens"] * pricing["input"] / 1_000_000
        + usage["output_tokens"] * pricing["output"] / 1_000_000
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_human(
    question: str,
    answer: str,
    hits: list[dict],
    usage: dict,
    model: str,
    cost_usd: float,
) -> None:
    print(f"\nQ: {question}\n")
    print("=" * 72)
    print(answer)
    print("=" * 72)
    print(f"\nSources used ({len(hits)} excerpts retrieved):")
    for i, h in enumerate(hits, 1):
        pages = ",".join(str(p) for p in h["page_nos"])
        print(
            f"  [{i}] {h['doc_id']}  pages=[{pages}]  "
            f"kind={h['doc_kind']}  score={h['score']:.3f}"
        )
    print(
        f"\nModel: {model}  |  tokens: {usage['input_tokens']} in / "
        f"{usage['output_tokens']} out  |  cost: ~${cost_usd:.4f}"
    )


def print_json(
    question: str,
    answer: str,
    hits: list[dict],
    usage: dict,
    model: str,
    cost_usd: float,
    refused: bool,
) -> None:
    """JSON shape designed for the eval pipeline in step 9."""
    print(json.dumps(
        {
            "question": question,
            "answer": answer,
            "refused": refused,
            "model": model,
            "hits": [
                {
                    "rank": i,
                    "chunk_id": h["chunk_id"],
                    "doc_id": h["doc_id"],
                    "doc_kind": h["doc_kind"],
                    "page_nos": h["page_nos"],
                    "case_nums": h["case_nums"],
                    "score": h["score"],
                }
                for i, h in enumerate(hits, 1)
            ],
            "usage": {**usage, "estimated_cost_usd": round(cost_usd, 6)},
        },
        indent=2,
        ensure_ascii=False,
    ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ask a question; get a cited answer grounded in the corpus."
    )
    ap.add_argument("question", help="The question to ask the corpus")
    ap.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"Chunks to retrieve before answering (default: {DEFAULT_TOP_K})",
    )
    ap.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Anthropic model id (default: {DEFAULT_MODEL})",
    )
    ap.add_argument(
        "--threshold",
        type=float,
        default=REFUSAL_THRESHOLD,
        help=f"Refuse if top-hit score below this (default: {REFUSAL_THRESHOLD})",
    )
    ap.add_argument(
        "--doc-kind",
        help="Restrict retrieval to a single doc_kind (e.g. newspaper, teletype)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of human-readable output",
    )
    args = ap.parse_args()

    # ---- 1. Retrieve ---------------------------------------------------
    index, _ = connect_to_index()
    filter_dict = {"doc_kind": {"$eq": args.doc_kind}} if args.doc_kind else None
    hits = search(
        index,
        query_text=args.question,
        top_k=args.top_k,
        filter=filter_dict,
    )

    # ---- 2. Decide: refuse if retrieval is too weak --------------------
    if should_refuse(hits, args.threshold):
        message = refusal_message(args.question, hits)
        if args.json:
            print_json(
                question=args.question,
                answer=message,
                hits=hits,
                usage={"input_tokens": 0, "output_tokens": 0},
                model=args.model,
                cost_usd=0.0,
                refused=True,
            )
        else:
            print(f"\nQ: {args.question}\n")
            print(message)
        return

    # ---- 3. Generate ---------------------------------------------------
    client = connect_to_anthropic()
    user_message = build_user_message(args.question, hits)
    answer, usage = call_claude(
        client,
        user_message=user_message,
        model=args.model,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
    )
    cost = estimate_cost_usd(usage, args.model)

    if args.json:
        print_json(args.question, answer, hits, usage, args.model, cost, refused=False)
    else:
        print_human(args.question, answer, hits, usage, args.model, cost)


if __name__ == "__main__":
    main()
