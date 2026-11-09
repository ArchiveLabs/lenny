"""Off-host proofs for docker/utils/image-hash.sh — the content-hash that makes
"rebuild only when necessary" exact rather than approximate (ArchiveLabs/lenny#144).

No Docker daemon is needed: the script is a pure function of the declared inputs,
so these tests exercise exactly the property the CI skip and the `make update`
pull both depend on — that the tag changes when, and only when, an image's own
inputs change. Red-first: each sensitivity test first mutates an input and watches
the hash move, then restores and watches it return.
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker" / "utils" / "image-hash.sh"
SERVICES = ["api", "reader", "admin", "opds"]


def tag(service, env=None):
    out = subprocess.run(
        ["bash", str(SCRIPT), "tag", service],
        cwd=ROOT, capture_output=True, text=True, env=env, check=True,
    )
    return out.stdout.strip()


def restore(path):
    subprocess.run(["git", "checkout", "--", str(path)], cwd=ROOT, check=True)


def append(path, text):
    with open(ROOT / path, "a") as fh:
        fh.write(text)


def test_tag_shape():
    for svc in SERVICES:
        t = tag(svc)
        assert t.startswith("inputs-"), t
        assert len(t) == len("inputs-") + 16


def test_deterministic():
    for svc in SERVICES:
        assert tag(svc) == tag(svc)


def test_services_distinct():
    tags = {tag(svc) for svc in SERVICES}
    assert len(tags) == len(SERVICES)


@pytest.mark.parametrize("service,input_path", [
    ("api", "requirements.txt"),
    ("reader", "docker/reader/Dockerfile"),
    ("admin", "docker/lenny-app/Dockerfile"),
    ("opds", "docker/opds-client/nginx.conf"),
])
def test_input_change_moves_only_its_own_hash(service, input_path):
    """Editing an input moves that service's hash and leaves the others fixed."""
    before = {svc: tag(svc) for svc in SERVICES}
    try:
        append(input_path, "\n# image-hash test marker\n")
        after = {svc: tag(svc) for svc in SERVICES}
    finally:
        restore(input_path)

    assert after[service] != before[service], f"{service} hash should move when {input_path} changes"
    for other in SERVICES:
        if other != service:
            assert after[other] == before[other], f"{other} must not move when {input_path} changes"

    # Restored to HEAD -> hash returns.
    assert tag(service) == before[service]


def test_api_hash_covers_tracked_dir_contents(tmp_path):
    """api COPYs lenny/** and alembic/**, so a change under those dirs must move
    the api hash (and nothing else)."""
    target = "lenny/__init__.py"
    assert (ROOT / target).exists()
    before = {svc: tag(svc) for svc in SERVICES}
    try:
        append(target, "\n# marker\n")
        after = {svc: tag(svc) for svc in SERVICES}
    finally:
        restore(target)
    assert after["api"] != before["api"]
    for other in ("reader", "admin", "opds"):
        assert after[other] == before[other]


def test_admin_build_arg_in_hash():
    """NEXT_PUBLIC_API_URL is inlined into the admin bundle, so a custom value
    must produce a distinct admin tag (cache miss -> local build) while leaving
    the other images untouched."""
    import os
    base = {svc: tag(svc) for svc in SERVICES}
    env = dict(os.environ, NEXT_PUBLIC_API_URL="https://openlibrary.example")
    assert tag("admin", env=env) != base["admin"]
    # The other images do not bake this value.
    for other in ("api", "reader", "opds"):
        assert tag(other, env=env) == base[other]
