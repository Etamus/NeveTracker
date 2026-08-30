from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
QUOTA_DATABASE_PATH = BACKEND_DIR / "provider_quota.db"

# Limits are deliberately conservative and represent only the free allowance.
# A reservation is never refunded: failed calls may still be billed externally.
PROVIDER_QUOTAS = {
    "bright_data": {
        "limit": 5_000,
        "period": "monthly",
        "unit": "requisições",
    },
    "apify": {
        "limit": 1_400,
        "period": "monthly",
        "unit": "resultados",
    },
    "serper": {
        "limit": 2_500,
        "period": "lifetime",
        "unit": "consultas",
    },
    "dataforseo": {
        "limit": 1_000,
        "period": "lifetime",
        "unit": "itens reservados",
    },
    "searchapi": {
        "limit": 100,
        "period": "lifetime",
        "unit": "consultas",
    },
    "mercado_livre": {
        "limit": None,
        "period": "unmetered",
        "unit": "requisições",
    },
}


class ProviderQuotaExceeded(RuntimeError):
    pass


def _period_key(period, now=None):
    current = now or datetime.now(timezone.utc)
    if period == "monthly":
        return current.strftime("%Y-%m")
    return "lifetime"


def _connection():
    QUOTA_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(QUOTA_DATABASE_PATH, timeout=30)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_quota_usage (
            provider TEXT NOT NULL,
            period_key TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, period_key)
        )
        """
    )
    return connection


def get_provider_quota(provider, now=None):
    config = PROVIDER_QUOTAS[provider]
    limit = config["limit"]
    if limit is None:
        return {
            "limit": None,
            "used": 0,
            "remaining": None,
            "unit": config["unit"],
            "period": config["period"],
            "period_key": None,
            "hard_locked": False,
        }

    period_key = _period_key(config["period"], now)
    connection = _connection()
    try:
        row = connection.execute(
            "SELECT used FROM provider_quota_usage WHERE provider = ? AND period_key = ?",
            (provider, period_key),
        ).fetchone()
    finally:
        connection.close()
    used = int(row[0]) if row else 0
    remaining = max(0, limit - used)
    return {
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "unit": config["unit"],
        "period": config["period"],
        "period_key": period_key,
        "hard_locked": remaining == 0,
    }


def reserve_provider_quota(provider, units=1, now=None):
    config = PROVIDER_QUOTAS[provider]
    limit = config["limit"]
    units = int(units)
    if units < 1:
        raise ValueError("A reserva de cota deve ser positiva.")
    if limit is None:
        return get_provider_quota(provider, now)

    period_key = _period_key(config["period"], now)
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    connection = _connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT used FROM provider_quota_usage WHERE provider = ? AND period_key = ?",
            (provider, period_key),
        ).fetchone()
        used = int(row[0]) if row else 0
        if used + units > limit:
            connection.rollback()
            remaining = max(0, limit - used)
            raise ProviderQuotaExceeded(
                f"Cota gratuita da integração esgotada ou insuficiente "
                f"({remaining} {config['unit']} restantes). A chamada foi bloqueada."
            )
        next_used = used + units
        connection.execute(
            """
            INSERT INTO provider_quota_usage (provider, period_key, used, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(provider, period_key) DO UPDATE SET
                used = excluded.used,
                updated_at = excluded.updated_at
            """,
            (provider, period_key, next_used, timestamp),
        )
        connection.commit()
    finally:
        connection.close()

    return {
        "limit": limit,
        "used": next_used,
        "remaining": limit - next_used,
        "unit": config["unit"],
        "period": config["period"],
        "period_key": period_key,
        "hard_locked": next_used == limit,
    }
