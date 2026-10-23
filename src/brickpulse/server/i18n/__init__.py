"""Copy-on-presentation localization: persisted recommendations remain canonical.

No SQL, scoring, grouping or cache key depends on the selected language. Technical
identifiers, resource names and raw evidence are intentionally never translated.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

LOCALES = ("en", "pt-BR", "es")


def negotiate(header: str | None) -> str:
    candidates = []
    for index, part in enumerate((header or "").split(",")):
        bits = part.strip().split(";")
        try:
            quality = next((float(b.strip()[2:]) for b in bits[1:] if b.strip().startswith("q=")), 1)
        except ValueError:
            continue
        if not 0 < quality <= 1:
            continue
        candidates.append((-quality, index, bits[0].lower().split("-")[0]))
    for _, _, base in sorted(candidates):
        if base in ("en", "pt", "es"):
            return {"pt": "pt-BR", "en": "en", "es": "es"}[base]
    return "en"


@lru_cache(maxsize=3)
def catalog(locale: str) -> dict:
    return json.loads((Path(__file__).parent / f"{locale}.json").read_text())


def message_refs(rule_id: str, evidence: dict) -> dict:
    """Versioned references survive JSON/Delta round trips, including raw params."""
    return {
        "version": 1, "title": f"rules.{rule_id}.title", "why": f"rules.{rule_id}.why",
        "how": f"rules.{rule_id}.how",
        "variant": str(evidence.get("tier") or evidence.get("case") or evidence.get("mode") or "default"),
        "params": {key: value for key, value in evidence.items()
                   if value is None or isinstance(value, (int, float, bool))},
    }


def present(value, locale: str):
    """Return a new JSON tree, never mutate cached/persisted financial objects."""
    if isinstance(value, list):
        return [present(item, locale) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: present(item, locale) for key, item in value.items()}
    rule_id = value.get("rule_id") or (value.get("id") if "thresholds" in value else None)
    translation = catalog(locale).get("rules", {}).get(rule_id)
    if translation:
        if "rule_title" in value:
            out["rule_title"] = translation["title"]
        if "title" in value and "thresholds" in value:
            out["title"] = translation["title"]
        if "why" in value:
            # English without an explicit translated presentation retains the old
            # API prose. The localized synopsis is additive, and full raw metrics
            # remain accessible to operators in all languages.
            evidence = value.get("evidence", {})
            out["presentation"] = {
                "locale": locale, "title": translation["title"],
                "why": translation["why"], "how": translation["how"],
                "caution": catalog(locale)["caution"],
                "sources": sorted(set(re.findall(r"(?:system\.)?[a-z_]+\.[a-z_]+", str(evidence.get("data_basis", ""))))),
                "legacy": "messages" not in evidence,
                "original_why": value.get("why", ""),
                "original_how": value.get("how", ""),
            }
            if locale != "en":
                out["why"] = translation["why"]
                out["how"] = translation["how"]
    if "action_label" in value and "option_count" in value:
        options = out.get("options", [])
        if value["option_count"] == 1 and options:
            out["action_label"] = options[0]["rule_title"]
        elif value["option_count"] == 1:
            # List rows omit options; keep the rule title rather than "1 options".
            rule = catalog(locale).get("rules", {}).get(value.get("top_rule_id")) or {}
            out["action_label"] = rule.get("title") or value["action_label"]
        else:
            out["action_label"] = catalog(locale)["options"].format(count=value["option_count"])
    return out


class LocalizationMiddleware:
    """ASGI JSON boundary. Applies after FastAPI validation/serialization.

    English contracts remain additive. SSE/static responses pass through without
    buffering. No user data enters a translation service.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        locale = negotiate(headers.get(b"accept-language", b"").decode("latin-1"))
        start = None
        chunks = []

        async def localize(message):
            nonlocal start
            if message["type"] == "http.response.start":
                is_json = any(k.lower() == b"content-type" and b"application/json" in v for k, v in message["headers"])
                if not is_json:
                    return await send(message)
                start = message
            elif message["type"] == "http.response.body" and start is not None:
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    body = b"".join(chunks)
                    try:
                        body = json.dumps(present(json.loads(body), locale), ensure_ascii=False, allow_nan=False).encode()
                    except (ValueError, TypeError):
                        pass
                    response_headers = [(k, v) for k, v in start["headers"] if k.lower() not in (b"content-length", b"content-language", b"vary")]
                    previous_vary = [v.decode("latin-1") for k, v in start["headers"] if k.lower() == b"vary"]
                    response_headers += [(b"content-length", str(len(body)).encode()), (b"content-language", locale.encode()), (b"vary", ", ".join(previous_vary + ["Accept-Language"]).encode())]
                    await send({**start, "headers": response_headers})
                    await send({"type": "http.response.body", "body": body})
            else:
                await send(message)
        await self.app(scope, receive, localize)
