from pathlib import Path
import os
import re
from threading import RLock

from dotenv import set_key

from services.provider_quota import get_provider_quota


BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_FILE_PATH = BACKEND_DIR / ".env"
SETTINGS_LOCK = RLock()

PROVIDER_CONFIG = {
    "bright_data": {
        "label": "Bright Data",
        "description": (
            "Google Shopping. Grátis: 5 mil requisições/mês; depois "
            "US$ 1,50 por 1.000 requisições bem-sucedidas."
        ),
        "credential_url": "https://brightdata.com/cp/zones",
        "category": "discovery",
        "credential_label": "API key",
        "key_env": "BRIGHT_DATA_API_KEY",
        "enabled_env": "BRIGHT_DATA_ENABLED",
        "mode_env": "BRIGHT_DATA_USAGE_MODE",
        "limit_env": "BRIGHT_DATA_MAX_RESULTS",
        "extra_env": "BRIGHT_DATA_SERP_ZONE",
        "extra_name": "zone",
        "extra_default": "",
    },
    "apify": {
        "label": "Apify",
        "description": (
            "Google Shopping. Plano grátis com US$ 5 em créditos mensais; "
            "o Actor configurado cobra US$ 3,50 por 1.000 resultados."
        ),
        "credential_url": "https://console.apify.com/account/integrations",
        "category": "discovery",
        "credential_label": "API token",
        "key_env": "APIFY_API_TOKEN",
        "enabled_env": "APIFY_ENABLED",
        "mode_env": "APIFY_USAGE_MODE",
        "limit_env": "APIFY_MAX_RESULTS",
        "extra_env": "APIFY_ACTOR_ID",
        "extra_name": "actor_id",
        "extra_default": "damilo/google-shopping-apify",
    },
    "serper": {
        "label": "Serper",
        "description": (
            "Google Shopping. Inclui 2.500 consultas grátis; depois usa créditos "
            "pré-pagos a partir de US$ 50 por 50 mil consultas."
        ),
        "credential_url": "https://serper.dev/",
        "category": "discovery",
        "credential_label": "API key",
        "key_env": "SERPER_API_KEY",
        "enabled_env": "SERPER_ENABLED",
        "mode_env": "SERPER_USAGE_MODE",
        "limit_env": "SERPER_MAX_RESULTS",
    },
    "dataforseo": {
        "label": "DataForSEO",
        "description": (
            "Produtos e sellers. Inclui US$ 1 em créditos de teste; depois US$ 0,001 por item, "
            "com recarga mínima de US$ 50 e sem mensalidade."
        ),
        "credential_url": "https://app.dataforseo.com/api-access",
        "category": "discovery",
        "credential_label": "API password",
        "key_env": "DATAFORSEO_PASSWORD",
        "enabled_env": "DATAFORSEO_ENABLED",
        "mode_env": "DATAFORSEO_USAGE_MODE",
        "limit_env": "DATAFORSEO_MAX_RESULTS",
        "extra_env": "DATAFORSEO_LOGIN",
        "extra_name": "login",
        "extra_default": "",
    },
    "searchapi": {
        "label": "SearchAPI (Google + Amazon)",
        "description": (
            "Google Shopping e Amazon Brasil. Inclui 100 consultas grátis; depois "
            "planos a partir de US$ 40/mês. Pode consumir até 4 consultas por SKU."
        ),
        "credential_url": "https://www.searchapi.io/dashboard",
        "category": "discovery",
        "credential_label": "API key",
        "key_env": "SEARCHAPI_API_KEY",
        "enabled_env": "SEARCHAPI_ENABLED",
        "mode_env": "SEARCHAPI_USAGE_MODE",
        "limit_env": "SEARCHAPI_MAX_RESULTS",
    },
    "mercado_livre": {
        "label": "Mercado Livre",
        "description": (
            "API oficial sem tarifa de uso publicada. Exige criar um aplicativo "
            "e autorizar a conta via OAuth para gerar o access token."
        ),
        "credential_url": "https://developers.mercadolivre.com.br/devcenter/",
        "category": "store",
        "credential_label": "Access token",
        "key_env": "MERCADO_LIVRE_ACCESS_TOKEN",
        "enabled_env": "MERCADO_LIVRE_API_ENABLED",
        "supports_usage_mode": False,
        "supports_limit": False,
    },
}

PROVIDER_ORDER = (
    "bright_data",
    "apify",
    "serper",
    "searchapi",
    "dataforseo",
    "mercado_livre",
)


def _as_bool(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_limit(value, default=20):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(100, max(10, parsed))


def _masked_secret(value):
    if not value:
        return None
    visible = value[-4:] if len(value) > 4 else value[-2:]
    return f"{'*' * 8}{visible}"


def _provider_settings(provider):
    config = PROVIDER_CONFIG.get(provider)
    if not config:
        raise ValueError("Integração desconhecida.")

    api_key = os.getenv(config["key_env"], "").strip()
    extra_env = config.get("extra_env")
    extra_value = (
        os.getenv(extra_env, config.get("extra_default", "")).strip()
        if extra_env
        else ""
    )
    mode_env = config.get("mode_env")
    usage_mode = os.getenv(mode_env, "fallback").strip().lower() if mode_env else "fallback"
    if usage_mode not in {"fallback", "always"}:
        usage_mode = "fallback"

    configured = bool(api_key and (extra_value if extra_env else True))
    enabled_value = os.getenv(config["enabled_env"])
    enabled = _as_bool(enabled_value) if enabled_value is not None else bool(api_key)
    result = {
        "provider": provider,
        "label": config["label"],
        "description": config["description"],
        "category": config["category"],
        "credential_label": config["credential_label"],
        "credential_url": config["credential_url"],
        "enabled": enabled,
        "configured": configured,
        "has_api_key": bool(api_key),
        "api_key_masked": _masked_secret(api_key),
        "usage_mode": usage_mode,
        "supports_usage_mode": config.get("supports_usage_mode", True),
        "supports_limit": config.get("supports_limit", True),
        "max_results": _bounded_limit(os.getenv(config.get("limit_env", ""))),
        "quota": get_provider_quota(provider),
    }
    if config.get("extra_name"):
        result[config["extra_name"]] = extra_value
    return result


def get_integration_settings():
    return {
        "items": [
            _provider_settings(provider)
            for provider in PROVIDER_ORDER
        ]
    }


def get_runtime_provider_settings(provider):
    settings = _provider_settings(provider)
    config = PROVIDER_CONFIG[provider]
    settings["api_key"] = os.getenv(config["key_env"], "").strip()
    return settings


def get_search_provider_names():
    return tuple(
        provider
        for provider in PROVIDER_ORDER
        for config in (PROVIDER_CONFIG[provider],)
        if config["category"] == "discovery"
    )


def _validate_extra_value(provider, value):
    value = (value or "").strip()
    if provider == "bright_data":
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{2,120}", value):
            raise ValueError("Informe um nome de zona Bright Data válido.")
    elif provider == "apify":
        if value and not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            value,
        ):
            raise ValueError("Use o Actor ID no formato usuario/actor.")
    elif provider == "dataforseo" and value and "@" not in value:
            raise ValueError("Informe o login da DataForSEO usado na conta.")
    return value


def _write_env(key, value):
    ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE_PATH.touch(exist_ok=True)
    set_key(str(ENV_FILE_PATH), key, str(value), quote_mode="always")
    os.environ[key] = str(value)


def update_integration_settings(provider, payload):
    config = PROVIDER_CONFIG.get(provider)
    if not config:
        raise ValueError("Integração desconhecida.")

    with SETTINGS_LOCK:
        current_key = os.getenv(config["key_env"], "").strip()
        if payload.clear_api_key:
            next_key = ""
        elif payload.api_key is not None and payload.api_key.strip():
            next_key = payload.api_key.strip()
        else:
            next_key = current_key

        extra_env = config.get("extra_env")
        extra_name = config.get("extra_name")
        current_extra = (
            os.getenv(extra_env, config.get("extra_default", "")).strip()
            if extra_env
            else ""
        )
        requested_extra = getattr(payload, extra_name, None) if extra_name else None
        next_extra = (
            _validate_extra_value(
                provider,
                requested_extra if requested_extra is not None else current_extra,
            )
            if extra_env
            else ""
        )

        if payload.enabled and (not next_key or (extra_env and not next_extra)):
            required_by_provider = {
                "bright_data": "API key e zona",
                "apify": "token e Actor ID",
                "dataforseo": "login e API password",
            }
            required = required_by_provider.get(provider, "a credencial")
            raise ValueError(f"Informe {required} antes de ativar a integração.")

        _write_env(config["key_env"], next_key)
        _write_env(config["enabled_env"], "true" if payload.enabled else "false")
        if config.get("mode_env"):
            _write_env(config["mode_env"], payload.usage_mode)
        if config.get("limit_env"):
            _write_env(config["limit_env"], _bounded_limit(payload.max_results))
        if extra_env:
            _write_env(extra_env, next_extra)

    return _provider_settings(provider)


def should_run_provider(provider, current_offer_count, missing_store_count=0):
    settings = get_runtime_provider_settings(provider)
    if not settings["enabled"] or not settings["configured"]:
        return False
    if settings["usage_mode"] == "always":
        return True
    if missing_store_count > 0:
        return True
    try:
        threshold = int(os.getenv("NEVETRACKER_EXTERNAL_FALLBACK_MIN_RESULTS", "8"))
    except (TypeError, ValueError):
        threshold = 8
    return current_offer_count < min(50, max(1, threshold))
