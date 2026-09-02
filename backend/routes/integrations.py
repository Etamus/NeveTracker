from fastapi import APIRouter, Depends, HTTPException

from models.auth import AuthUser
from schemas.integration import IntegrationUpdateRequest
from services.auth_service import require_developer
from services.integration_settings import (
    get_integration_settings,
    update_integration_settings,
)


integration_router = APIRouter()


@integration_router.get("")
def list_integrations(_actor: AuthUser = Depends(require_developer)):
    return get_integration_settings()


@integration_router.put("/{provider}")
def update_integration(
    provider: str,
    payload: IntegrationUpdateRequest,
    _actor: AuthUser = Depends(require_developer),
):
    try:
        result = update_integration_settings(provider, payload)
        if provider == "mercado_livre":
            from services.mercado_livre_api import search_mercado_livre

            search_mercado_livre.cache_clear()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
