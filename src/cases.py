"""
The cases this corpus covers.

Every chunk carries a `case` so retrieval can be scoped to one investigation.
Without it the only thing separating one case's documents from another's is a
filename convention, and a question about an escape would draw from whichever
case happened to score highest — answered confidently, with citations, from the
wrong file. That is the exact failure this system exists to prevent, and it is
silent.

This registry lives at the top of src/ rather than inside ingestion because both
sides need it: ingestion rejects an unknown --case at the point of writing, and
the API validates the filter against the same list. One definition, so the two
cannot drift.

Registering a case here is deliberately a code change. It happens rarely, it
wants review, and the alternative -- accepting any string -- reintroduces the
trap doc_kind had: a typo silently matches nothing and the refusal that follows
reads as "the corpus has nothing on this" rather than "you misspelled it".

To add a case: add the slug and display name, then ingest with --case <slug>.
"""

# slug -> human-readable name. Slugs appear in chunk metadata and in API
# filters, so they are lowercase and hyphenated, and should not change once
# documents have been indexed under them.
CASES: dict[str, str] = {
    "bundy": "Ted Bundy",
}


def slugs() -> list[str]:
    return sorted(CASES)


def is_known(slug: str) -> bool:
    return slug in CASES


def display_name(slug: str) -> str:
    return CASES.get(slug, slug)


def validate(slug: str) -> str:
    """Return the slug, or raise ValueError naming the valid ones.

    Callers turn this into whatever their layer needs -- a CLI exit, or a 422 --
    but the message is written once here so every layer says the same thing.
    """
    if not is_known(slug):
        raise ValueError(
            f"Unknown case {slug!r}. Known cases: {', '.join(slugs())}. "
            f"Add it to src/cases.py before ingesting under it."
        )
    return slug
