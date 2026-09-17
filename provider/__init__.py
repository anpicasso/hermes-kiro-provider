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


def _seed_core_model_catalog() -> None:
    """Make `hermes model` show an arrow-key picker instead of a raw "Model name:" prompt.

    ``_api_key_provider_model_list`` (hermes_cli/model_setup_flows.py) resolves an api_key
    provider's list from the core's ``_PROVIDER_MODELS`` curated dict, then models.dev, then a
    ``GET {base_url}/v1/models`` probe. It never consults the plugin's own ``fetch_models()`` or
    ``fallback_models``, and Kiro serves no OpenAI-style /models endpoint — so all three miss and
    the flow falls through to free-text input. Seeding the dict the core does read gives the
    picker its list. Best-effort: a rename upstream just restores the text prompt.
    """
    try:
        from hermes_cli.models import _PROVIDER_MODELS
        models = list(_catalog_models() or _FALLBACK_MODELS)
        if models:
            _PROVIDER_MODELS.setdefault("kiro", models)
    except Exception:
        pass  # ponytail: picker is a nicety; login and inference do not depend on it


_seed_core_model_catalog()


def _register_commands() -> str | None:
    """Register `hermes kiro` and `/kiro` from this model-provider plugin.

    Hermes routes ``kind: model-provider`` manifests to providers/ discovery and never calls
    ``register(ctx)`` with a live context (#111258), so the only way to own our own commands is to
    build the PluginContext ourselves. That is internal API: on any signature change we degrade to
    the standalone entrypoint instead of taking the provider down with us.
    """
    try:
        from hermes_cli.plugins import PluginContext, get_plugin_manager
        from hermes_cli.plugins_manifest import PluginManifest
        import commands as _commands

        ctx = PluginContext(
            PluginManifest(name="kiro", version="0.1.7", kind="model-provider", source="user"),
            get_plugin_manager())
        ctx.register_cli_command(
            name="kiro", help="Kiro login, usage and status", setup_fn=_commands.setup_parser,
            handler_fn=_commands.handle,
            description="Kiro IAM Identity Center device-code login.")
        ctx.register_command(
            name="kiro", handler=_commands.handle_slash, description="Kiro status and usage",
            args_hint="status|usage", argument_mode="text")
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


_COMMAND_REGISTRATION_ERROR = _register_commands()
if _COMMAND_REGISTRATION_ERROR:
    print(f"kiro: `hermes kiro` and `/kiro` could not register ({_COMMAND_REGISTRATION_ERROR}).\n"
          f"Log in with:\n  python {_HERE / 'commands.py'} login", file=sys.stderr)


def register(ctx) -> None:
    # ponytail: providers/ discovery already registered the profile and the commands above;
    # this only lets the generic plugin doctor load the same manifest successfully.
    return None
