"""Native Kiro provider: direct HTTPS, no local proxy and no kiro-cli."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from providers import register_provider
from providers.base import ProviderProfile

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from client import KiroClient, list_model_ids  # noqa: E402

_FALLBACK_MODELS = ("claude-sonnet-4.5", "claude-haiku-4.5", "gpt-5.6-terra")
_CATALOG_MODELS = _FALLBACK_MODELS
_CATALOG_AT = 0.0


def _catalog_models() -> tuple:
    global _CATALOG_MODELS, _CATALOG_AT
    if time.monotonic() - _CATALOG_AT >= 300:
        try:
            models = tuple(list_model_ids())
            if models:
                _CATALOG_MODELS = models
        finally:
            _CATALOG_AT = time.monotonic()
    return _CATALOG_MODELS


class KiroProfile(ProviderProfile):
    @property
    def fallback_models(self) -> tuple:
        return _catalog_models()

    @fallback_models.setter
    def fallback_models(self, value: tuple) -> None:
        global _CATALOG_MODELS
        _CATALOG_MODELS = tuple(value or _FALLBACK_MODELS)

    def create_client(self, **kwargs):
        return KiroClient(**kwargs)

    def fetch_models(self, **kwargs):
        return list_model_ids() or list(self.fallback_models)


profile = KiroProfile(
    name="kiro",
    aliases=("kiro-native",),
    display_name="Kiro (native)",
    description="Kiro via direct HTTPS and IAM Identity Center; plugin-owned credentials.",
    signup_url="https://kiro.dev",
    auth_type="api_key",
    env_vars=("KIRO_AUTH",),  # sentinel; real credentials stay in ~/.hermes/kiro
    base_url="https://runtime.us-east-1.kiro.dev",
    hostname="runtime.us-east-1.kiro.dev",
    supports_health_check=False,
    supports_vision=False,
    fallback_models=_FALLBACK_MODELS,
    default_aux_model="claude-haiku-4.5",
)
register_provider(profile)


def register(ctx) -> None:
    # ponytail: model-provider discovery performs the real registration above;
    # this makes the generic plugin doctor load the same manifest successfully.
    return None
