"""
Web Step 1 — Acquire court opinions from the CourtListener API.

This is the web equivalent of probe.py + ocr.py: it turns a remote source into
raw material on disk. We deliberately DON'T parse or chunk here. Acquisition
(network, needs a token, non-deterministic) is kept separate from parsing
(offline, deterministic, re-runnable) so that iterating on the parser never
re-hits the API — same split the PDF path uses (ocr once, clean/chunk many).

Why CourtListener and not Justia: Justia sits behind a Cloudflare challenge that
a scripted client can't clear. CourtListener exposes the same opinions through a
free REST API — structured JSON, real metadata, no bot wall. You need a free
token: https://www.courtlistener.com/help/api/rest/  → put it in .env as
    COURTLISTENER_TOKEN=...

For each case we save ONE JSON file to data/raw/opinions/<slug>.json containing
both the opinion record (the text) and its cluster record (case name, citation,
date, docket, judges). data/raw is gitignored, so nothing here is committed.

Usage (run from project root):
    python -m src.ingestion.fetch_opinions
    python -m src.ingestion.fetch_opinions --only bundy-1984-chi-omega
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data" / "raw" / "opinions"

API = "https://www.courtlistener.com/api/rest/v4"

# The five Bundy v. State opinions, newest work last. opinion_id is the
# CourtListener /opinions/<id>/ primary key (holds the text); the cluster URL
# on that record gives us the authoritative case metadata. Slugs double as the
# source_stem downstream, so they show up in chunk ids and citations — keep
# them short, stable, and human-readable.
CASES = [
    ("bundy-1984-chi-omega",     1719324, "455 So. 2d 330  — Chi Omega direct appeal"),
    ("bundy-1985-leach",         1817368, "471 So. 2d 9    — Kimberly Leach appeal"),
    ("bundy-1986-postconviction", 1875754, "490 So. 2d 1258 — postconviction appeal"),
    ("bundy-1986-companion",     1709742, "497 So. 2d 1209 — companion habeas denial"),
    ("bundy-1989-final",         1111125, "538 So. 2d 445  — final Rule 3.850 appeal"),
]

# Transient failures are real here (we already ate one ReadTimeout while probing).
# Same policy as embed_chunks.py: back off on network/5xx, fail fast on 4xx.
MAX_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 2
TIMEOUT_SECONDS = 60
# The free tier caps at 5 requests/min. We make 2 requests per case (opinion +
# cluster), so a full 5-case run needs ~10 requests and WILL hit the cap. We
# ride it out by waiting on each 429 rather than failing; this bounds how many
# times we're willing to wait before giving up (throttle waits are ~60s each).
MAX_THROTTLE_WAITS = 8


def load_token() -> str:
    load_dotenv(REPO_ROOT / ".env")
    tok = os.getenv("COURTLISTENER_TOKEN")
    if not tok:
        sys.exit(
            "COURTLISTENER_TOKEN is not set.\n"
            "  1. Register (free): https://www.courtlistener.com/sign-in/\n"
            "  2. Profile -> Developer -> Create API token\n"
            "  3. Add to .env:  COURTLISTENER_TOKEN=<token>\n"
        )
    return tok


def get_json(client: httpx.Client, url: str) -> dict:
    """
    GET one URL, retrying transient failures.

    Three status classes, three behaviors:
      - 429 (throttled): NOT a bug — the free tier caps at 5 req/min. Wait the
        server-advertised Retry-After, then retry. Given a wait, don't burn a
        retry budget meant for flaky networks; loop patiently.
      - other 4xx (bad token/id): our fault. Fail fast so the bug is visible.
      - 5xx / network: transient. Exponential backoff.
    """
    throttle_waits = 0
    net_attempt = 0
    while True:
        try:
            r = client.get(url, timeout=TIMEOUT_SECONDS, follow_redirects=True)
            if r.status_code == 429:
                if throttle_waits >= MAX_THROTTLE_WAITS:
                    sys.exit(f"Still throttled after {MAX_THROTTLE_WAITS} waits on {url}")
                throttle_waits += 1
                wait = int(r.headers.get("Retry-After", 60)) + 1
                print(f"  throttled (5/min cap) — waiting {wait}s then retrying ...",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            if 400 <= r.status_code < 500:
                sys.exit(f"HTTP {r.status_code} for {url}\n  {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            net_attempt += 1
            if net_attempt >= MAX_RETRIES:
                raise
            backoff = INITIAL_BACKOFF_SECONDS * (2 ** (net_attempt - 1))
            print(f"  retry {net_attempt}/{MAX_RETRIES} after {backoff}s ({type(e).__name__})",
                  file=sys.stderr)
            time.sleep(backoff)


def fetch_case(client: httpx.Client, slug: str, opinion_id: int) -> dict:
    """Pull the opinion record + its cluster, bundle into one raw payload."""
    opinion = get_json(client, f"{API}/opinions/{opinion_id}/")
    cluster_url = opinion.get("cluster")
    cluster = get_json(client, cluster_url) if cluster_url else {}
    return {
        "slug": slug,
        "opinion_id": opinion_id,
        "fetched_from": f"{API}/opinions/{opinion_id}/",
        "opinion": opinion,
        "cluster": cluster,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Bundy court opinions from CourtListener.")
    ap.add_argument("--only", help="Fetch just one case by slug (e.g. bundy-1984-chi-omega)")
    args = ap.parse_args()

    cases = CASES
    if args.only:
        cases = [c for c in CASES if c[0] == args.only]
        if not cases:
            slugs = ", ".join(c[0] for c in CASES)
            sys.exit(f"Unknown slug '{args.only}'. Choose from: {slugs}")

    token = load_token()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Token {token}", "User-Agent": "Case_File_AI research"}

    with httpx.Client(headers=headers) as client:
        for slug, opinion_id, label in cases:
            print(f"[{slug}]  {label}")
            payload = fetch_case(client, slug, opinion_id)

            # Which text field carried the goods? (all 5 use html_lawbox today,
            # but record it so the parser's fallback order stays honest.)
            op = payload["opinion"]
            text_field = next(
                (f for f in ("plain_text", "html_lawbox", "html_columbia", "html",
                             "html_with_citations")
                 if op.get(f)),
                None,
            )
            cl = payload["cluster"]
            cites = ", ".join(
                f"{c.get('volume','')} {c.get('reporter','')} {c.get('page','')}".strip()
                for c in (cl.get("citations") or [])
            ) or "?"
            print(f"    case='{cl.get('case_name','?')}'  filed={cl.get('date_filed','?')}  "
                  f"cite={cites}")
            print(f"    text_field={text_field}  chars={len(op.get(text_field) or '')}")

            out = RAW_DIR / f"{slug}.json"
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"    -> {out.relative_to(REPO_ROOT)}\n")

    print(f"[OK] Saved {len(cases)} opinion(s) to {RAW_DIR.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
