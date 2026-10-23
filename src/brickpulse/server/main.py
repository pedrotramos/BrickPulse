"""FastAPI application factory.

Serves the JSON API under ``/api`` and the built React SPA (Vite output in
``static/``) for everything else, with a catch-all so client-side routes deep
link correctly. ``/health`` is the Databricks Apps readiness gate.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import ROUTERS
from .config import get_settings
from .logging import configure_logging, get_logger
from brickpulse import __version__

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
#: The shell must be revalidated on every load, or a browser keeps running the
#: previous release's bundle after a deploy. Hashed assets never change in place.
NO_CACHE = {"Cache-Control": "no-cache"}
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


class _HashedAssets(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.update(IMMUTABLE)
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger("brickpulse")
    log.info(
        "Starting BrickPulse (compute_mode=%s, auth_mode=%s)",
        settings.compute_mode,
        settings.auth_mode,
    )

    app = FastAPI(title="BrickPulse", version=__version__)
    from .i18n import LocalizationMiddleware
    app.add_middleware(LocalizationMiddleware)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log.exception("Request failed: %s", request.url.path)
        return JSONResponse({"detail": "Analysis unavailable", "code": "analysis_unavailable"}, status_code=503)
    for router in ROUTERS:
        from .deps import get_identity
        app.include_router(router, dependencies=[Depends(get_identity)])

    @app.get("/health")
    def health() -> dict:
        return {"status": "healthy", "brand": "BrickPulse", "version": __version__}

    if STATIC_DIR.exists():
        # Serve hashed assets directly...
        if (STATIC_DIR / "assets").exists():
            app.mount("/assets", _HashedAssets(directory=STATIC_DIR / "assets"), name="assets")

        # ...and fall back to index.html for SPA client-side routes.
        @app.get("/{full_path:path}")
        def spa(full_path: str):
            if full_path.startswith("api/"):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            candidate = (STATIC_DIR / full_path).resolve()
            if full_path and candidate.is_relative_to(STATIC_DIR.resolve()) and candidate.is_file():
                return FileResponse(candidate, headers=NO_CACHE)
            if INDEX_HTML.exists():
                return FileResponse(INDEX_HTML, headers=NO_CACHE)
            return JSONResponse({"detail": "Not found"}, status_code=404)
    else:
        # No SPA bundle (e.g. API-only deploy before `npm run build`): send the
        # root to the interactive API docs so the app is still usable.
        @app.get("/")
        def root():
            return RedirectResponse(url="/docs")

    return app


app = create_app()
