from fastapi import APIRouter, Depends, HTTPException

from models.auth import AuthUser
from schemas.integration import IntegrationUpdateRequest
from pydantic import BaseModel
from services.auth_service import require_developer
from services.integration_settings import (
    get_integration_settings,
    update_integration_settings,
)


integration_router = APIRouter()


@integration_router.get('/accounts/{store}')
def browser_account_status(store: str, _actor: AuthUser = Depends(require_developer)):
    from services.marketplace_session import session_status
    from services.marketplace_intervention import enabled,pending
    try:return {**session_status(store),'enabled':enabled(store),'intervention':pending(store)}
    except ValueError as exc:raise HTTPException(400,str(exc))


class BrowserAccountUpdate(BaseModel):
    enabled:bool


@integration_router.put('/accounts/{store}')
def configure_browser_account(store:str,payload:BrowserAccountUpdate,_actor:AuthUser=Depends(require_developer)):
    from services.marketplace_intervention import configure
    try:configure(store,payload.enabled)
    except ValueError as exc:raise HTTPException(400,str(exc))
    return browser_account_status(store,_actor)


@integration_router.post('/accounts/{store}/connect')
def connect_browser_account(store: str, _actor: AuthUser = Depends(require_developer)):
    from services.marketplace_session import start_validation
    try:return start_validation(store)
    except ValueError as exc:raise HTTPException(409,str(exc))


@integration_router.post('/accounts/{store}/confirm')
def confirm_browser_account(store: str, _actor: AuthUser = Depends(require_developer)):
    from services.marketplace_session import finish_validation
    try:return finish_validation(store)
    except ValueError as exc:raise HTTPException(400,str(exc))


@integration_router.get('/mercado-livre/session')
def mercado_livre_session(_actor: AuthUser = Depends(require_developer)):
    from services.mercado_livre_session import session_status
    return session_status()


@integration_router.post('/mercado-livre/session')
def validate_mercado_livre_session(_actor: AuthUser = Depends(require_developer)):
    from services.mercado_livre_session import start_validation
    try:
        return start_validation()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


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
