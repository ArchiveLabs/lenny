"""The migration chain must stay a single line: unique revision ids, one head.

A reused revision id makes alembic report a cycle and `make update` fail.
"""
import pathlib
import re

VERSIONS = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _graph():
    revs, downs = [], set()
    for f in VERSIONS.glob("*.py"):
        text = f.read_text()
        rev = re.search(r"^revision(?::[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
        down = re.search(r"^down_revision(?::[^=]+)?\s*=\s*(.+)$", text, re.M)
        assert rev and down, f"{f.name}: no revision/down_revision"
        revs.append(rev.group(1))
        downs.update(re.findall(r"['\"]([0-9a-zA-Z_]+)['\"]", down.group(1)))
    return revs, downs


def test_revision_ids_are_unique():
    revs, _ = _graph()
    dupes = {r for r in revs if revs.count(r) > 1}
    assert not dupes, f"duplicate revision ids: {dupes}"


def test_single_head():
    revs, downs = _graph()
    heads = set(revs) - downs
    assert len(heads) == 1, f"expected one head, got {heads}"
