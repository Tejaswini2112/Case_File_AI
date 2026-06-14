"""
Step 9 — Golden eval runner.

Runs every question in tests/eval_set.jsonl against ask.py and scores it on
four checks. Pass-rate across the suite is the regression-test signal: if a
later change to chunking, embedding, or prompting drops the pass-rate, the
change is bad even if the code still runs.

The checks (each yields True / False / None-not-applicable):

  refusal_correct      Did the system refuse iff the question expected refusal?
                       This is the single most important check — false refusals
                       and false answers are the two failure modes that erode
                       user trust fastest.

  top_doc_match        Did at least one expected doc_id appear in the retrieved
                       hits? Tests RETRIEVAL quality, separate from generation.

  citations_present    Does the answer contain inline citations in our format?
                       Tests that Claude is following the citation contract.

  citations_grounded   Does every citation point to a doc_id that was actually
                       retrieved? Catches hallucinated cites — the bug where
                       the model invents a doc-id that wasn't in context.

A question passes if every applicable check is True.

Usage (run from project root):
    python tests/run_eval.py
    python tests/run_eval.py --eval-set tests/eval_set.jsonl
    python tests/run_eval.py --json    # machine-readable, for CI pipelines
    python tests/run_eval.py --only eval-001,eval-007    # subset
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVAL_SET = REPO_ROOT / "tests" / "eval_set.jsonl"
ASK_SCRIPT = REPO_ROOT / "src" / "ask.py"
PYTHON_EXE = REPO_ROOT / ".venv" / "Scripts" / "python.exe"


# Inline-citation regex. Matches:
#   [bundy-part-01__doc-003, p.6]
#   [bundy-part-01__doc-003, p.6; p.7]
#   [bundy-part-01__doc-003, pages 6-7]
# Captures the doc-id for grounding checks. Tolerant of whitespace.
CITATION_RE = re.compile(
    r"\[(bundy-part-\d+__doc-\d+)\s*,\s*p[ages.\s]*\d+",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------


def call_ask(question: str, timeout: int = 60) -> dict:
    """
    Invoke ask.py via subprocess in JSON mode and return the parsed result.

    Why subprocess (not import): we want true end-to-end testing — the eval
    must exercise CLI parsing, env loading, retrieval, refusal, and generation
    exactly as a real user would. Importing the module would skip the CLI
    layer and risk passing evals that fail in production.

    Trade-off: ~1s of subprocess startup overhead per question. At 10
    questions that's ~10s of pure overhead — fine. If the suite grows to
    100+ questions we'd revisit (likely a parallel pool over subprocesses).
    """
    cmd = [
        str(PYTHON_EXE),
        str(ASK_SCRIPT),
        question,
        "--json",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ask.py failed (exit {result.returncode}):\n"
            f"STDERR:\n{result.stderr}\n"
            f"STDOUT:\n{result.stdout}"
        )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_refusal_correct(actual: dict, expected_behavior: str) -> bool:
    """Did the refusal decision match expectation?"""
    actual_refused = bool(actual.get("refused", False))
    expected_refused = expected_behavior == "refuse"
    return actual_refused == expected_refused


def check_top_doc_match(actual: dict, expected_doc_ids: list[str]) -> bool | None:
    """
    Was at least one expected doc retrieved? None = no expectation set.

    We accept ANY of the expected docs (not all), because for narrative
    questions like "Why did Bundy escape?" multiple newspaper articles are
    each defensible top hits.
    """
    if not expected_doc_ids:
        return None
    retrieved = {h["doc_id"] for h in actual.get("hits", [])}
    return any(eid in retrieved for eid in expected_doc_ids)


def check_citations_present(actual: dict) -> bool | None:
    """Are there inline citations in the answer? None = system refused."""
    if actual.get("refused"):
        return None
    return bool(CITATION_RE.search(actual.get("answer", "")))


def check_citations_grounded(actual: dict) -> bool | None:
    """
    Do all cited doc-ids appear in the retrieved set? None = refused or
    no citations. False = the model cited at least one doc that wasn't
    actually in its context — i.e. it hallucinated a reference.
    """
    if actual.get("refused"):
        return None
    cited = {m.group(1) for m in CITATION_RE.finditer(actual.get("answer", ""))}
    if not cited:
        return None  # citations_present already caught this
    retrieved = {h["doc_id"] for h in actual.get("hits", [])}
    return cited.issubset(retrieved)


# ---------------------------------------------------------------------------
# Per-entry runner
# ---------------------------------------------------------------------------


def score_entry(entry: dict, actual: dict) -> dict:
    """Run all four checks. Return a flat dict of results + a pass bool."""
    checks = {
        "refusal_correct": check_refusal_correct(actual, entry["expected_behavior"]),
        "top_doc_match":   check_top_doc_match(actual, entry.get("expected_doc_ids", [])),
        "citations_present":   check_citations_present(actual),
        "citations_grounded":  check_citations_grounded(actual),
    }
    # A question passes if no check is False (None = N/A, doesn't fail)
    passed = all(v is not False for v in checks.values())
    return {"passed": passed, "checks": checks}


def run_one(entry: dict) -> dict:
    """Execute one eval question end to end. Returns a result dict."""
    t0 = time.perf_counter()
    actual = call_ask(entry["question"])
    elapsed = time.perf_counter() - t0
    score = score_entry(entry, actual)
    return {
        "id": entry["id"],
        "category": entry["category"],
        "question": entry["question"],
        "expected_behavior": entry["expected_behavior"],
        "actual_refused": bool(actual.get("refused", False)),
        "passed": score["passed"],
        "checks": score["checks"],
        "top_hit_score": actual["hits"][0]["score"] if actual.get("hits") else None,
        "top_hit_doc_id": actual["hits"][0]["doc_id"] if actual.get("hits") else None,
        "answer_preview": (actual.get("answer") or "")[:200],
        "cost_usd": actual.get("usage", {}).get("estimated_cost_usd", 0),
        "elapsed_s": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------


def load_eval_set(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"Eval set not found: {path}")
    return [
        json.loads(line)
        for line in path.open(encoding="utf-8")
        if line.strip()
    ]


def run_suite(entries: list[dict]) -> list[dict]:
    results = []
    for i, entry in enumerate(entries, 1):
        print(f"[{i}/{len(entries)}] {entry['id']}: {entry['question'][:60]}...",
              end="", flush=True)
        try:
            result = run_one(entry)
        except Exception as e:
            print(f" ERROR")
            print(f"    {type(e).__name__}: {e}", file=sys.stderr)
            result = {
                "id": entry["id"],
                "category": entry["category"],
                "question": entry["question"],
                "expected_behavior": entry["expected_behavior"],
                "passed": False,
                "error": f"{type(e).__name__}: {e}",
            }
        else:
            tick = "PASS" if result["passed"] else "FAIL"
            print(f" {tick}  ({result['elapsed_s']}s, ${result['cost_usd']:.4f})")
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------


def print_summary(results: list[dict]) -> None:
    n = len(results)
    n_passed = sum(1 for r in results if r.get("passed"))
    total_cost = sum(r.get("cost_usd", 0) for r in results)

    print()
    print("=" * 72)
    print(f"SUITE RESULT:  {n_passed}/{n} passed")
    print(f"Total cost:    ${total_cost:.4f}")
    print(f"Avg per query: ${total_cost / n:.4f}" if n else "")
    print("=" * 72)

    # Per-check tally
    check_names = ["refusal_correct", "top_doc_match", "citations_present", "citations_grounded"]
    print("\nPer-check breakdown:")
    for cname in check_names:
        true_count = sum(
            1 for r in results
            if r.get("checks", {}).get(cname) is True
        )
        false_count = sum(
            1 for r in results
            if r.get("checks", {}).get(cname) is False
        )
        na_count = n - true_count - false_count
        print(f"  {cname:<22}  {true_count} pass  {false_count} fail  {na_count} N/A")

    # Surface the failures
    failures = [r for r in results if not r.get("passed")]
    if failures:
        print(f"\nFailures ({len(failures)}):")
        for f in failures:
            print(f"  - {f['id']}: {f['question'][:60]}")
            if "error" in f:
                print(f"      ERROR: {f['error']}")
            else:
                failed_checks = [
                    name for name, v in f["checks"].items() if v is False
                ]
                print(f"      failed: {', '.join(failed_checks)}")
                if f.get("answer_preview"):
                    print(f"      answer: {f['answer_preview'][:120]}...")
    else:
        print("\nAll questions passed — system is healthy.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the golden eval set against ask.py.")
    ap.add_argument(
        "--eval-set",
        type=Path,
        default=DEFAULT_EVAL_SET,
        help=f"Eval set JSONL path (default: {DEFAULT_EVAL_SET.relative_to(REPO_ROOT)})",
    )
    ap.add_argument(
        "--only",
        help="Comma-separated list of eval ids to run (e.g. eval-001,eval-007)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON results instead of human summary",
    )
    args = ap.parse_args()

    entries = load_eval_set(args.eval_set)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        entries = [e for e in entries if e["id"] in wanted]
        if not entries:
            sys.exit(f"No eval entries matched: {args.only}")

    print(f"Running {len(entries)} eval questions ...\n")
    results = run_suite(entries)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print_summary(results)

    # Exit code: 0 if all passed, 1 otherwise. Useful in CI.
    sys.exit(0 if all(r.get("passed") for r in results) else 1)


if __name__ == "__main__":
    main()
