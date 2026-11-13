"""Readium serves any book by its base64 path with no auth, so it (and the
reader) must never be published on a host port, and Garage's S3 API stays on
loopback. nginx in lenny_api is the only way in."""

import pathlib
import re

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "compose.yaml"


def _service(name):
    # shortcut: indentation-based block split, no PyYAML in the image; fine while compose.yaml uses 2-space services
    text = COMPOSE.read_text()
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S|\Z)", text, re.M | re.S)
    assert match, f"service {name} not found in compose.yaml"
    return match.group(1)


def test_readium_has_no_published_port():
    assert "ports:" not in _service("readium")


def test_reader_has_no_published_port():
    assert "ports:" not in _service("reader")


def test_object_store_binds_loopback_only():
    ports = re.findall(r'^\s+- "([^"]+)"', _service("s3").split("ports:", 1)[1], re.M)
    assert ports and all(p.startswith("127.0.0.1:") for p in ports), ports
