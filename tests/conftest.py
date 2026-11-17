import json
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def spec() -> dict:
    return json.loads((FIXTURES / "openapi.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def v3_spec() -> dict:
    """Official Arcane 3.0.1 OpenAPI export. See tests/test_v3_compat.py for provenance."""
    return json.loads((FIXTURES / "openapi-v3.0.1.json").read_text(encoding="utf-8"))


@pytest.fixture
def make_arcane_client() -> Callable[[Callable[[httpx2.Request], httpx2.Response]], httpx2.AsyncClient]:
    def _make(handler):
        return httpx2.AsyncClient(
            base_url="http://arcane.test/api",
            headers={"X-API-Key": "test-key"},
            transport=httpx2.MockTransport(handler),
        )

    return _make
