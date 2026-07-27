"""
Unit tests for the deterministic pieces of the pipeline.

Fast, offline, repeatable — no API calls, no Pinecone, no cost. They lock in the
behavior of the pure logic so a refactor that breaks it fails INSTANTLY. This is
the complement to run_eval.py: the eval measures whole-system answer quality
(slow, costs money, LLM-variable); these check that each small part is correct.

Run:  pytest tests/test_units.py -v
"""

import sys
from pathlib import Path

# Put the repo root on the import path so `src...` and `tests...` resolve when
# pytest collects this file (same trick ask.py uses to run from any cwd).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup

from src.ingestion.chunk_documents import count_tokens, split_sentences
from src.ingestion.chunk_opinions import ARG_START, block_text_and_pages, pack_blocks
from tests.run_eval import CITATION_RE, is_refusal


def _longest_shared_run(a: str, b: str) -> int:
    """Length (in words) of the longest suffix of a that is a prefix of b."""
    aw, bw = a.split(), b.split()
    return max((k for k in range(1, min(len(aw), len(bw)) + 1) if aw[-k:] == bw[:k]), default=0)


# 1) Refusal detection — the two-way refusal fix (flag OR prose, but not a
#    hedged answer that cites a source).
def test_is_refusal():
    assert is_refusal({"refused": True, "answer": "anything"}) is True
    assert is_refusal({
        "refused": False,
        "answer": "The provided documents do not contain information about this.",
    }) is True
    # Hedged but real answer that DOES cite a source -> NOT a refusal.
    assert is_refusal({
        "refused": False,
        "answer": "The excerpts do not contain a single ruling, but they refer to it "
                  "[bundy-1989-final, p.447].",
    }) is False
    assert is_refusal({
        "refused": False,
        "answer": "Bundy escaped [bundy-part-01__doc-013, p.21].",
    }) is False


# 2) Citation detector recognizes BOTH id formats and rejects non-citations.
def test_citation_regex():
    assert CITATION_RE.search("...here [bundy-part-01__doc-003, pages 6-7].")   # FBI style
    assert CITATION_RE.search("...here [bundy-1984-chi-omega, p.334].")          # opinion style
    assert CITATION_RE.search("just [some bracketed text]") is None
    m = CITATION_RE.search("[bundy-1984-chi-omega, p.334]")
    assert m.group(1) == "bundy-1984-chi-omega"                                  # captures the doc-id


# 3) Opinion chunker overlap — split pieces must share a ~word cushion.
def test_pack_blocks_overlap():
    # Three ~300-word paragraphs (each ~390 tokens) force a size split at target=500.
    paras = [" ".join(f"w{i}_{p}" for i in range(300)) for p in range(3)]
    blocks = [(t, [p + 1], count_tokens(t)) for p, t in enumerate(paras)]
    chunks = pack_blocks(blocks, target=500, overlap_words=100)
    assert len(chunks) >= 3                                  # it actually split
    for a, b in zip(chunks, chunks[1:]):
        assert _longest_shared_run(a["text"], b["text"]) > 0  # neighbors overlap


# 4) Star-pagination tracking — marker advances the page and is dropped from text.
def test_star_pagination_tracking():
    html = '<div><p>before text <span class="star-pagination">*335</span> after text</p></div>'
    p = BeautifulSoup(html, "html.parser").find("p")
    state = {"page": 334}
    text, pages = block_text_and_pages(p, state)
    assert "*335" not in text                # the marker itself is not prose
    assert "before text" in text and "after text" in text
    assert pages == [334, 335]               # both sides of the break tracked
    assert state["page"] == 335              # running page advanced


# 5) Sentence splitter abbreviation guard — "Dr." is not a sentence end.
def test_split_sentences_abbreviation_guard():
    sents = split_sentences("Dr. Smith arrived at noon. He left later.")
    assert len(sents) == 2
    assert sents[0].startswith("Dr. Smith arrived")


# 6) Argument-opening patterns fire on real openers, not on ordinary prose.
def test_arg_start_patterns():
    assert ARG_START.match("as his first point on appeal bundy argues that ...")
    assert ARG_START.match("next appellant contends that the testimony ...")
    assert ARG_START.match("bundy's next point on appeal is that ...")
    assert not ARG_START.match("the intruder entered the sorority house.")
