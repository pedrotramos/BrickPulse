"""Identity + scope abstractions.

An :class:`IdentityProvider` turns an incoming HTTP request into an
:class:`Identity` (who + role + accessible workspaces) and yields the access
token used to open the SQL connection. Two implementations exist:

* ``obo``  — token is the logged-in user's (dev + user-scoped governance).
* ``sp``   — the app runs as a service principal; a user->scope mapping decides
  which workspaces each user may see.

:class:`Scope` is derived from an Identity and is the ONLY thing the data layer
trusts for workspace filtering — never client-supplied query params.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import Identity

PLATFORM_ADMIN = "platform_admin"
WORKSPACE_ADMIN = "workspace_admin"


@dataclass
class Scope:
    """Server-side workspace scope. Platform admins see everything."""

    all_workspaces: bool
    workspace_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_identity(cls, identity: Identity) -> Scope:
        if identity.all_workspaces or identity.role == PLATFORM_ADMIN:
            return cls(all_workspaces=True, workspace_ids=[])
        return cls(all_workspaces=False, workspace_ids=list(identity.workspace_ids))

    def narrowed_to(self, workspace_id: str) -> Scope:
        """This scope restricted to one workspace — for a "drill into X" view.

        Narrowing only ever REMOVES access. ``workspace_id`` arrives from the client
        (a path param), so it is treated as a filter applied *inside* the
        server-derived scope, never as a grant: a workspace admin asking for a
        workspace they do not own gets an empty scope (which
        ``scoping.where_clause`` renders as ``IN (NULL)``), not that workspace's
        data. A platform admin, who legitimately sees everything, gets exactly the
        one they asked for.

        Raises :class:`~finops.data.scoping.ScopeError` for a non-numeric id, so a
        malformed request fails at the route (as a 400) rather than later while
        building SQL, where the same rejection reads as a server fault.
        """
        from ..data.scoping import ScopeError

        wid = str(workspace_id)
        if not wid.isdigit():
            raise ScopeError(f"Invalid workspace id: {workspace_id!r}")
        if not self.all_workspaces and wid not in {str(w) for w in self.workspace_ids}:
            return Scope(all_workspaces=False, workspace_ids=[])
        return Scope(all_workspaces=False, workspace_ids=[wid])


class IdentityProvider(ABC):
    """Resolves the caller's identity and access token from a request."""

    @abstractmethod
    def resolve(self, request) -> Identity:
        raise NotImplementedError

    @abstractmethod
    def access_token(self, request) -> str | None:
        raise NotImplementedError
