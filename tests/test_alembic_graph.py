"""The migration chain must stay a single line: unique revision ids, one head.

A reused revision id makes alembic report a cycle and `make update` fail.
"""
import pathlib
import re

VERSIONS = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _graph():
    revs, downs, parents = [], set(), {}
    for f in VERSIONS.glob("*.py"):
        text = f.read_text()
        rev = re.search(r"^revision(?::[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
        down = re.search(r"^down_revision(?::[^=]+)?\s*=\s*(.+)$", text, re.M)
        assert rev and down, f"{f.name}: no revision/down_revision"
        revs.append(rev.group(1))
        parents[rev.group(1)] = re.findall(r"['\"]([0-9a-zA-Z_]+)['\"]", down.group(1))
        downs.update(parents[rev.group(1)])
    return revs, downs, parents


def test_revision_ids_are_unique():
    revs, _, _ = _graph()
    dupes = {r for r in revs if revs.count(r) > 1}
    assert not dupes, f"duplicate revision ids: {dupes}"


def test_single_head():
    revs, downs, _ = _graph()
    heads = set(revs) - downs
    assert len(heads) == 1, f"expected one head, got {heads}"


def test_every_revision_is_reachable_from_the_head():
    """Each parent exists, every revision is reachable from the head, and every path down ends
    at a root. Merge revisions (two parents) are fine: the repo has one, cfe7b1f7f7f5."""
    revs, downs, parents = _graph()
    missing = downs - set(revs)
    assert not missing, f"down_revision points at unknown revisions: {missing}"
    (head,) = set(revs) - downs
    seen, todo = set(), [head]
    while todo:
        rev = todo.pop()
        if rev not in seen:
            seen.add(rev)
            todo.extend(parents[rev])
    assert seen == set(revs), f"not reachable from the head: {set(revs) - seen}"

    def ends_at_a_root(rev, path=()):
        assert rev not in path, f"cycle: {' -> '.join(path + (rev,))}"
        return all(ends_at_a_root(p, path + (rev,)) for p in parents[rev])
    ends_at_a_root(head)
