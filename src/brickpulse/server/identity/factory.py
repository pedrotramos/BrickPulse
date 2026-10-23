"""Pick the identity provider from ``FINOPS_AUTH_MODE``."""

from __future__ import annotations

from ..config import Settings
from .base import IdentityProvider
from .obo import OboIdentityProvider
from .service_principal import SpIdentityProvider


def make_identity_provider(settings: Settings) -> IdentityProvider:
    if settings.auth_mode == "sp":
        return SpIdentityProvider(settings)
    if settings.auth_mode == "obo":
        return OboIdentityProvider(settings)
    raise ValueError(f"Unknown FINOPS_AUTH_MODE: {settings.auth_mode!r}")
