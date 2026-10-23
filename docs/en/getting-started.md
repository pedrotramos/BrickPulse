# Getting started

**English** · [Español](../es/getting-started.md) · [Português](../pt-BR/getting-started.md)

This guide sets up BrickPulse on your machine and runs it on synthetic data. You don't need a Databricks
workspace for any of it. To install BrickPulse in a workspace, continue with the
[deployment guide](deployment.md).

## Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| Python | 3.10 or newer (3.13 is what the maintainers use) | Backend, rules, pipeline, tests |
| Node.js | 20.19 or newer | Building and serving the React frontend |
| npm | Ships with Node | Frontend dependencies |
| Git | Any recent version | Cloning the repository |
| [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html) | A recent release with bundle support | Deployment only |

## 1. Clone and install

```sh
git clone https://github.com/pedrotramos/BrickPulse.git
cd BrickPulse

python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'

npm --prefix frontend ci
```

`.[dev]` installs the runtime dependencies plus pytest, httpx, ruff and `build`. The deploy script uses
`build` to package the wheel.

## 2. Run the demo

```sh
npm run dev
```

Open <http://127.0.0.1:5173>. Development mode runs the real FastAPI app in memory with **synthetic data**,
and the UI labels it as demo data. The data reaches the browser through a development-only Vite
transport. It doesn't connect to any workspace, and none of this code ships in the deployed app.

Use the language menu to switch between English, Spanish and Portuguese.

## 3. Run the backend on its own (optional)

To call the API directly, for example with `curl` or from another frontend:

```sh
PYTHONPATH=src BRICKPULSE_COMPUTE_MODE=demo .venv/bin/python -m brickpulse
```

The server binds to `0.0.0.0` on `DATABRICKS_APP_PORT` (default `8000`) and serves the compiled UI from
`src/brickpulse/server/static`.

> [!WARNING]
> Demo mode and the development identity mode trust local defaults. Never expose them as a production
> installation. See [security](security.md).

## 4. Run the checks

These are the same checks CI runs:

```sh
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
npm --prefix frontend run typecheck
npm run build
```

For the browser end-to-end tests, install Chromium once:

```sh
(cd frontend && npx playwright install chromium)
npm --prefix frontend test
```

[Testing](../TESTING.md) describes what each suite covers.

## 5. Build the wheel (optional)

```sh
npm run build
.venv/bin/python scripts/build_wheel.py
```

The React build is served by FastAPI and packed into the wheel, so production never needs Node. The
script builds from a clean staging directory, so stale assets from earlier builds never get in.

## Next steps

- [Deploy to a workspace](deployment.md)
- [Configure pricing, tabs and rules](configuration.md)
- [Contribute a change](../../CONTRIBUTING.md)
