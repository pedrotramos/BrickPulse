"""Run the API and bundled frontend on the Databricks Apps assigned port."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("brickpulse.server.main:app", host="0.0.0.0", port=int(os.getenv("DATABRICKS_APP_PORT", "8000")))
