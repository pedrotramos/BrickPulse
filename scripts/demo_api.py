"""Offline development transport for the REAL FastAPI app, over JSON-lines.

Only synthetic demo data is allowed. Vite owns the loopback HTTP socket; this
process needs no listening socket. Production never uses this test transport.
"""
import json
import logging
import os
import sys

os.environ["BRICKPULSE_COMPUTE_MODE"] = "demo"
os.environ["BRICKPULSE_AUTH_MODE"] = "obo"
os.environ["BRICKPULSE_APPS_TAB_ENABLED"] = "true"
os.environ["BRICKPULSE_GENIE_COSTS_TAB_ENABLED"] = "true"
from fastapi.testclient import TestClient
from brickpulse.server.main import app

logging.disable(logging.CRITICAL)
client = TestClient(app, raise_server_exceptions=False)
for line in sys.stdin:
    request = {}
    try:
        request = json.loads(line)
        response = client.request(request["method"], request["path"], headers=request.get("headers"), content=request.get("body") or None)
        print(json.dumps({"id": request["id"], "status": response.status_code, "headers": dict(response.headers), "body": response.text}), flush=True)
    except Exception:
        print(json.dumps({"id": request.get("id"), "status": 500, "headers": {"content-type": "application/json"}, "body": '{"detail":"Demo transport error"}'}), flush=True)
