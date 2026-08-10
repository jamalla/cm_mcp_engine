"""Engine tests run against pinned fixtures, never a sibling checkout.

The engine and the contracts live in separate repositories, so these tests must
pass in a clone of this repo alone -- CI has no cm_mcp_contracts next door.
`tests/fixtures/contracts/` holds a pinned copy of representative contracts
covering both binding types, a destructive propose-apply tool, and a multi-tool
package.

Real contracts are exercised separately, and more meaningfully, by
`scripts/check_registry.py` against the artifact cm_mcp_contracts publishes.
"""

import os
import socket
import threading
import time
from pathlib import Path

import pytest

FIXTURE_CONTRACTS = Path(__file__).parent / "fixtures" / "contracts"

# All three set before cm_engine.config is imported anywhere, because it calls
# load_dotenv -- and load_dotenv does not override a variable that already exists,
# which is what makes assigning here sufficient.
#
# Contracts resolve to the pinned fixtures, not a sibling checkout on a
# developer's machine.
os.environ["CM_CONTRACTS_DIR"] = str(FIXTURE_CONTRACTS)

# The suite never leaves this checkout. A developer who sets DEV_OFFLINE=0 in .env
# to try their real store would otherwise point every http test at production --
# the calls would carry their token, and the 401s would read like engine bugs
# rather than like a misconfigured test run.
os.environ["DEV_OFFLINE"] = "1"

# And a placeholder credential, so a real one cannot reach a test process even by
# accident. The offline upstream accepts anything.
os.environ["SALLA_ACCESS_TOKEN"] = "test-token-not-a-real-one"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# The suite runs its own upstream on its own port. `dev.ps1` holds 8787 while a
# developer has the demo up, and the two must not meet: binding fails, and the
# instance already there carries state from whatever has been run against it --
# a category this suite deletes stays deleted, so the next run reads a 404 and
# blames the engine. The upstream table picks this up because it is read before
# cm_engine.config is imported.
os.environ["MOCK_API_PORT"] = str(_free_port())


@pytest.fixture(scope="session")
def mock_upstream():
    """Run the offline upstream in-process for tests that exercise http bindings.

    Without this a test can pass only because a dev happens to have the demo
    stack running -- which is how the propose-apply test quietly depended on a
    background process until a clean checkout failed it.
    """
    import httpx
    import uvicorn

    from cm_engine.config import MOCK_API_PORT
    from cm_engine.mock_upstream import app

    base = f"http://127.0.0.1:{MOCK_API_PORT}"

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=MOCK_API_PORT, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{MOCK_API_PORT}/healthz", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        pytest.fail(f"mock upstream never came up on :{MOCK_API_PORT}")

    yield base

    server.should_exit = True
    thread.join(timeout=5)
