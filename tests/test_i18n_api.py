import copy
import json
import re
from pathlib import Path

import pytest

from brickpulse.rules import list_rules
from brickpulse.server.i18n import catalog, negotiate, present
from brickpulse.server.identity import Scope


@pytest.mark.parametrize("header,expected", [
    (None, "en"), ("fr,de", "en"), ("pt-PT", "pt-BR"),
    ("es-MX,en;q=0.3", "es"), ("es;q=0,en;q=.5", "en"),
    ("pt;q=oops,es;q=.8", "es"), ("pt;q=.2,en;q=.9", "en"),
])
def test_negotiation(header, expected):
    assert negotiate(header) == expected


def _base_keys(path: Path) -> set[str]:
    # Plural forms are per-language (CLDR): pt-BR needs `_zero` where English does
    # not, so parity is checked on the base key.
    return {re.sub(r"_(zero|one|two|few|many|other)$", "", k) for k in json.loads(path.read_text())}


def test_catalogs_and_numeric_roundtrip(source):
    ids = {r.id for r in list_rules()}
    frontend = Path("frontend/src/locales")
    keys = _base_keys(frontend / "en.json")
    original = source.load(Scope(all_workspaces=True))[0].model_dump(mode="json")
    canonical = copy.deepcopy(original)
    for locale in ("en", "pt-BR", "es"):
        assert set(catalog(locale)["rules"]) == ids
        assert _base_keys(frontend / f"{locale}.json") == keys
        translated = present(json.loads(json.dumps(original)), locale)
        for key, value in canonical.items():
            if isinstance(value, (int, float)):
                assert translated[key] == value
        assert translated["evidence"] == canonical["evidence"]
        assert translated["finding_key"] == canonical["finding_key"]
        assert translated["presentation"]["locale"] == locale
    assert original == canonical


@pytest.mark.parametrize("locale", ["en", "pt-BR", "es"])
@pytest.mark.parametrize("path", ["/api/config", "/api/me", "/api/account/overview", "/api/workspaces", "/api/rules",
    "/api/recommendation-groups/page", "/api/tagging", "/api/experimental", "/api/spend",
    "/api/serverless-case", "/api/savings-realized", "/api/anomalies", "/api/data-health"])
def test_api_surfaces(client, locale, path):
    response = client.get(path, headers={"Accept-Language": locale})
    assert response.status_code == 200, response.text
    assert response.headers["content-language"] == locale
    assert "Accept-Language" in response.headers["vary"]


@pytest.mark.parametrize("query", ["page=0", "page_size=101", "page_size=0", "sort=invalid", "q=" + "x" * 201])
def test_bad_pagination(client, query):
    assert client.get("/api/recommendation-groups/page?" + query).status_code == 422


def test_static_deep_links_and_api_not_found(client):
    assert client.get("/recommendations").status_code == 200
    assert client.get("/api/not-an-endpoint").status_code == 404
    response = client.get("/%2e%2e/%2e%2e/pyproject.toml")
    assert "[project]" not in response.text


def test_optional_tabs_off(client):
    assert client.get("/api/apps").status_code == 404
    assert client.get("/api/genie").status_code == 404


def test_sp_scope_is_fail_closed(client, monkeypatch):
    from brickpulse.server import deps
    from brickpulse.server.config import Settings
    from brickpulse.server.identity.service_principal import SpIdentityProvider
    monkeypatch.setenv("BRICKPULSE_USER_SCOPE_JSON", json.dumps({"owner@example.com": {"role": "platform_admin"}}))
    provider = SpIdentityProvider(Settings(auth_mode="sp", compute_mode="demo"))
    monkeypatch.setattr(deps, "get_identity_provider", lambda: provider)
    assert client.get("/api/account/overview").status_code == 403
    assert client.get("/api/data-health", headers={"x-forwarded-email": "other@example.com"}).status_code == 403
    assert client.get("/api/account/overview", headers={"x-forwarded-email": "owner@example.com"}).status_code == 200
    monkeypatch.setenv("BRICKPULSE_USER_SCOPE_JSON", "[]")
    assert SpIdentityProvider._load_scope_map() == {}


def test_scope_cannot_be_broadened(source):
    scope = Scope(all_workspaces=False, workspace_ids=["123"])
    assert scope.narrowed_to("456").workspace_ids == []
    assert source.fetch(scope.narrowed_to("456")) == []
