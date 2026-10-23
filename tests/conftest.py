import os

os.environ["BRICKPULSE_COMPUTE_MODE"] = "demo"
os.environ["BRICKPULSE_AUTH_MODE"] = "obo"

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from brickpulse.server.main import app
    with TestClient(app) as value:
        yield value


@pytest.fixture
def source():
    from brickpulse.server.config import Settings
    from brickpulse.server.data.demo_source import DemoRecommendationSource
    return DemoRecommendationSource(Settings(compute_mode="demo"), None)
