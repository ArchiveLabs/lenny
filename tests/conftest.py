import sys
from pathlib import Path

# `scripts/` holds the operator CLI, which is worth testing like any
# other code path an operator depends on.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param

def pytest_addoption(parser):
    """`--lenny <url>` points the browser tests at a running node.

    Registered here because pytest only collects addoption hooks from conftest
    files. Without the flag those tests skip, so the default suite stays
    hermetic.
    """
    parser.addoption("--lenny", action="store", default=None,
                     help="base URL of a running Lenny node for browser tests")
