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
import threading
import time
from pathlib import Path

import pytest

FIXTURE_CONTRACTS = Path(__file__).parent / "fixtures" / "contracts"

# Set before cm_engine.config is imported anywhere, so the cascade resolves here
# rather than finding a sibling checkout on a developer's machine.
os.environ["CM_CONTRACTS_DIR"] = str(FIXTURE_CONTRACTS)


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

    yield f"http://127.0.0.1:{MOCK_API_PORT}"

    server.should_exit = True
    thread.join(timeout=5)
