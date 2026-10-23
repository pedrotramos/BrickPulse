"""Proxy to the Databricks Genie Conversation API — the context-aware chat.

This is the fix for the panel's original blind spot: the embedded Genie iframe is
cross-origin and cannot be told what the surrounding page is showing, so on a
resource page "why is this idle?" got a contextless answer. Here the *backend*
holds the conversation, so it prepends the workspace / resource the page is on to
the user's question before sending it to Genie — the answer is in-context, renders
in the app's own chat bubble, and no dashboard-embedding allowlist is needed.

Auth: the call runs as the **signed-in user** (the OBO token forwarded by
Databricks Apps, or ``DATABRICKS_TOKEN`` locally), so Genie honours that user's
Unity Catalog grants — the same governance the rest of the app relies on. Unlike
the SQL warehouse (where the forwarded token hangs, so the app uses its SP — see
``sql_client``), the REST API accepts the user token happily. If no user token is
available it falls back to the app service principal via the SDK config.

Everything here is best-effort and time-boxed: Genie is an optional augmentation,
so a failure returns a stated error the panel can show, never a 500 that breaks
the page.
"""

from __future__ import annotations

import time

from ..config import Settings
from ..logging import get_logger
from ..models import GenieAskRequest, GenieAskResponse

log = get_logger(__name__)

# The Conversation API is asynchronous: POST returns a message id, then the
# message is polled until it leaves the in-progress states. Genie answers are
# usually a few seconds; cap the wait so a wedged space can't hang a request.
_POLL_TIMEOUT_S = 55
_POLL_INTERVAL_S = 1.5
# Statuses that mean "still working" — anything else ends the poll loop.
_PENDING_STATUSES = {
    "FETCHING_METADATA",
    "FILTERING_CONTEXT",
    "ASKING_AI",
    "PENDING_WAREHOUSE",
    "EXECUTING_QUERY",
    "SUBMITTED",
    "IN_PROGRESS",
}
# Cap the result table we hand back — the panel shows a preview, not a data export.
_MAX_ROWS = 100


class GenieConversationClient:
    """Thin, synchronous client over the Genie Conversation REST API."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.space_id = settings.genie_space_id_resolved

    # --- auth / transport ----------------------------------------------------
    def _host(self) -> str:
        host = self.settings.server_hostname
        if not host:
            from databricks.sdk.core import Config

            host = (Config().host or "").replace("https://", "").replace("http://", "")
        return host.rstrip("/")

    def _headers(self, user_token: str | None) -> dict:
        """Bearer headers, preferring the caller's OBO token so Genie answers under
        the user's own UC grants; falls back to the app SP when none is forwarded."""
        token = user_token or self.settings.databricks_token
        if token:
            return {"Authorization": f"Bearer {token}"}
        # Deployed with no forwarded user token: authenticate as the app SP.
        from databricks.sdk.core import Config

        cfg = Config()
        # cfg.authenticate() returns the ready-to-use auth header(s) for this request.
        return dict(cfg.authenticate())

    def _request(self, method: str, path: str, user_token: str | None, json: dict | None = None):
        import requests

        url = f"https://{self._host()}{path}"
        resp = requests.request(
            method, url, headers=self._headers(user_token), json=json, timeout=40
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # --- prompt construction -------------------------------------------------
    @staticmethod
    def _contextualize(req: GenieAskRequest) -> str:
        """Prepend the page's context so a bare "why is this idle?" resolves.

        Only ever ADDS context in front of the user's own words — never rewrites or
        drops them — so the user still sees their question answered, just grounded
        in the resource/workspace the page is showing. This is guidance to Genie,
        NOT a security boundary: UC grants (enforced under the caller's token) are
        what actually scope the data.
        """
        bits: list[str] = []
        if req.resource_type and (req.resource_name or req.resource_id):
            name = req.resource_name or req.resource_id
            loc = f" in workspace {req.workspace_name or req.workspace_id}" if (
                req.workspace_name or req.workspace_id
            ) else ""
            bits.append(
                f'The user is looking at the {req.resource_type} "{name}"'
                f' (id {req.resource_id}){loc}.'
            )
        elif req.workspace_id or req.workspace_name:
            bits.append(
                f"The user is looking at workspace {req.workspace_name or req.workspace_id}."
            )
        if not bits:
            return req.question
        return " ".join(bits) + " Answer this question about it: " + req.question

    # --- the one public call -------------------------------------------------
    def ask(self, req: GenieAskRequest, user_token: str | None) -> GenieAskResponse:
        """Send one turn and return Genie's completed answer (text + any table).

        Starts a new conversation on the first turn (no ``conversation_id``) and
        posts a follow-up otherwise, so multi-turn context is preserved.
        """
        if not self.space_id:
            return GenieAskResponse(error="Genie space is not configured.")

        content = self._contextualize(req)
        base = f"/api/2.0/genie/spaces/{self.space_id}"
        try:
            if req.conversation_id:
                started = self._request(
                    "POST",
                    f"{base}/conversations/{req.conversation_id}/messages",
                    user_token,
                    json={"content": content},
                )
                conversation_id = req.conversation_id
            else:
                started = self._request(
                    "POST", f"{base}/start-conversation", user_token,
                    json={"content": content},
                )
                conversation_id = started.get("conversation_id") or (
                    started.get("conversation") or {}
                ).get("id")
            message_id = started.get("message_id") or (started.get("message") or {}).get("id")
            if not conversation_id or not message_id:
                return GenieAskResponse(error="Genie did not start a conversation.")

            message = self._poll(base, conversation_id, message_id, user_token)
            return self._to_response(base, conversation_id, message_id, message, user_token)
        except Exception as exc:  # network / auth / API error — never a 500 here
            log.warning("Genie ask failed: %s", exc)
            return GenieAskResponse(
                conversation_id=req.conversation_id,
                error="Genie could not answer that just now. Please try again.",
            )

    # --- polling + result shaping -------------------------------------------
    def _poll(self, base: str, cid: str, mid: str, user_token: str | None) -> dict:
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            message = self._request(
                "GET", f"{base}/conversations/{cid}/messages/{mid}", user_token
            )
            if message.get("status") not in _PENDING_STATUSES:
                return message
            time.sleep(_POLL_INTERVAL_S)
        # Ran out the clock while still pending — report a timeout rather than
        # letting a pending status fall through as an empty (contentless) answer.
        return {"status": "TIMEOUT"}

    def _to_response(
        self, base: str, cid: str, mid: str, message: dict, user_token: str | None
    ) -> GenieAskResponse:
        status = message.get("status")
        out = GenieAskResponse(conversation_id=cid, message_id=mid)
        if status == "TIMEOUT":
            out.error = "Genie took too long to answer. Please try again."
            return out
        if status in ("FAILED", "CANCELLED"):
            out.error = message.get("error") or "Genie could not answer that."
            return out

        # A completed message carries attachments: a text answer and/or a query.
        attachments = message.get("attachments") or []
        text_parts: list[str] = []
        query_attachment = None
        for att in attachments:
            if att.get("text", {}).get("content"):
                text_parts.append(att["text"]["content"])
            if att.get("query"):
                query_attachment = att
        # Some responses put the text directly on the message.
        if not text_parts and message.get("content"):
            text_parts.append(message["content"])
        out.text = "\n\n".join(text_parts).strip()

        if query_attachment:
            q = query_attachment["query"]
            out.query = q.get("query") or q.get("description")
            self._attach_result(
                out, base, cid, mid, query_attachment.get("attachment_id"), user_token
            )
        return out

    def _attach_result(
        self, out: GenieAskResponse, base: str, cid: str, mid: str,
        attachment_id: str | None, user_token: str | None,
    ) -> None:
        """Best-effort fetch of the query result table for the answer bubble.

        A missing/oversized result never fails the turn — the text answer stands on
        its own; the table is a bonus."""
        try:
            if attachment_id:
                path = f"{base}/conversations/{cid}/messages/{mid}/attachments/{attachment_id}/query-result"
            else:
                path = f"{base}/conversations/{cid}/messages/{mid}/query-result"
            result = self._request("GET", path, user_token)
        except Exception as exc:
            log.info("Genie query-result unavailable: %s", exc)
            return

        # The statement result nests under statement_response.result / .manifest.
        sr = result.get("statement_response") or result
        manifest = sr.get("manifest") or {}
        schema = (manifest.get("schema") or {}).get("columns") or []
        out.columns = [c.get("name", "") for c in schema]
        data = (sr.get("result") or {}).get("data_array") or []
        if len(data) > _MAX_ROWS:
            out.rows = data[:_MAX_ROWS]
            out.truncated = True
        else:
            out.rows = data
