"""The reader is the heaviest container in the stack and the one that OOMs small
hosts, so two properties of its image are worth pinning.

Measured 2026-11-25 on the published image, 10 identical requests to each
container: 357 MiB as shipped, 48.8 MiB with both of these in place.
"""

import pathlib
import re

DOCKERFILE = pathlib.Path(__file__).resolve().parent.parent / "docker/reader/Dockerfile"


def _text():
    return DOCKERFILE.read_text()


def test_cmd_runs_next_directly():
    """`pnpm start` cost a 110 MB Node process whose only job was to spawn an `sh`
    that spawned Next, and left pnpm as PID 1 fielding the signals Next wants for
    a graceful shutdown."""
    cmd = re.search(r"^CMD (\[.*\])", _text(), re.M)
    assert cmd, "no CMD in the reader Dockerfile"
    assert not re.search(r'"(pnpm|npm|yarn|npx)"', cmd.group(1)), (
        f"reader CMD starts Next through a package manager: {cmd.group(1)}"
    )


def test_cloudflare_dev_init_is_guarded():
    """thorium-web calls initOpenNextCloudflareForDev() at module load, and
    `next start` loads next.config too -- so without this guard every production
    container boots a Cloudflare Workers runtime (83 MB RSS) for dev bindings
    that do not exist. An upstream bump that drops the patch must fail here, not
    quietly cost 300 MB a node."""
    text = _text()
    assert "initOpenNextCloudflareForDev" in text, (
        "the next.config guard is gone from the reader Dockerfile"
    )
    assert "NODE_ENV !== " in text, (
        "initOpenNextCloudflareForDev is patched but no longer guarded on NODE_ENV"
    )
