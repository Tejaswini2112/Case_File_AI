"""
Where everything on disk lives.

One module owns the layout so the rest of the code asks rather than assumes.
Before this, seven modules each built their own paths out of string literals,
which made the layout an unwritten agreement between them: changing it meant
seven coordinated edits and no way to tell which one had been missed.

The layout groups by case first and source second:

    data/cases/<case>/
        raw/scans/          source PDFs
        raw/opinions/       fetched court-opinion JSON
        ocr/<stem>/         per-document OCR output
        web/<slug>/         parsed opinion chunks
        batch-state.json    what a batch run finished

Case first because the operations that matter once there is more than one case
are all case-shaped: re-index this case, delete this case, hand this case to
another machine, copy this case to object storage. Grouped by source, each of
those is a pattern match across several trees; grouped by case, each is one
directory. That last one is the reason to bother now rather than later --
"cases/btk/..." is a storage key, while a tree keyed on how a file was
acquired has no prefix meaning "everything about BTK".

CASEFILE_DATA_ROOT relocates all of it. It exists because the point of naming
the layout in one place is being able to move it: a second disk, a mounted
volume, or a scratch directory for a test run that must not touch real data.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Absolute paths are used as given; a relative one resolves against the repo, so
# CASEFILE_DATA_ROOT=tmp/data behaves the same wherever it is invoked from.
DATA_ROOT = Path(os.getenv("CASEFILE_DATA_ROOT") or REPO_ROOT / "data")
if not DATA_ROOT.is_absolute():
    DATA_ROOT = REPO_ROOT / DATA_ROOT

CASES_ROOT = DATA_ROOT / "cases"


def case_root(case: str) -> Path:
    """Everything belonging to one investigation."""
    return CASES_ROOT / case


def scans_dir(case: str) -> Path:
    """Source PDFs, as downloaded."""
    return case_root(case) / "raw" / "scans"


def opinions_dir(case: str) -> Path:
    """Fetched court-opinion JSON, one file per opinion."""
    return case_root(case) / "raw" / "opinions"


def ocr_dir(case: str, stem: str) -> Path:
    """Per-document OCR output, named after the source PDF.

    Keyed on the filename stem rather than anything derived, because doc_id is
    built from it, chunk_id from doc_id, and the index's vector ids from
    chunk_id. Renaming a source file would orphan every vector it produced and
    change every citation string that names it.
    """
    return case_root(case) / "ocr" / stem


def web_dir(case: str, slug: str) -> Path:
    """Parsed chunks for one fetched opinion."""
    return case_root(case) / "web" / slug


def batch_state_path(case: str) -> Path:
    """What a batch run finished, per case.

    Per case rather than global: two cases ingesting concurrently would
    otherwise write the same file, and a state file naming files from several
    cases cannot be reasoned about when one of them is deleted.
    """
    return case_root(case) / "batch-state.json"


def known_case_dirs() -> list[Path]:
    """Case directories that actually exist on disk.

    Distinct from src/cases.py, which lists cases the code will accept. The two
    can legitimately differ -- a case registered but not yet ingested, or data
    left behind after a case was removed from the registry -- and telling them
    apart is worth being able to do.
    """
    if not CASES_ROOT.exists():
        return []
    return sorted(p for p in CASES_ROOT.iterdir() if p.is_dir())
