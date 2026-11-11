from datetime import datetime
import base64
import binascii
import re

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from database import get_db
from models.auth import AuthSession, AuthUser
from schemas.auth import LoginRequest, UserCreateRequest, UserUpdateRequest, UserPreferencesRequest
from services.auth_service import (
    ROLE_DEVELOPER,
    ROLE_ADMINISTRATOR,
    FULL_ACCESS_ROLES,
    ROLE_GOVERNOR,
    ROLE_VIEWER,
    apply_user_role,
    create_session,
    get_auth_session,
    get_current_user,
    normalize_username,
    require_admin,
    set_password,
    user_role,
    user_payload,
    verify_password,
)


auth_router = APIRouter()
AVATAR_PATTERN = re.compile(r"^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)$")
MAX_AVATAR_BYTES = 400 * 1024


@auth_router.put('/me/preferences')
def save_own_preferences(payload: UserPreferencesRequest, actor=Depends(get_current_user), db: Session = Depends(get_db)):
    actor.logistics_postal_code = payload.logistics_postal_code
    db.commit()
    return {'logistics_postal_code': actor.logistics_postal_code}


def _find_user_or_404(db, user_id):
    user = db.query(AuthUser).filter(AuthUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")
    return user


def _ensure_target_access(actor, target):
    actor_role = user_role(actor)
    target_role = user_role(target)
    if target_role in FULL_ACCESS_ROLES and actor_role not in FULL_ACCESS_ROLES:
        raise HTTPException(status_code=403, detail="O usuario principal e protegido.")
    if target_role == ROLE_GOVERNOR and actor_role not in FULL_ACCESS_ROLES and target.id != actor.id:
        raise HTTPException(status_code=403, detail="Governantes so podem alterar a propria conta.")


def _requested_role(payload, default=ROLE_VIEWER):
    if payload.role is not None:
        return payload.role
    if payload.is_admin is not None:
        return ROLE_GOVERNOR if payload.is_admin else ROLE_VIEWER
    return default


def _validated_avatar(value):
    if value is None or value == "":
        return None
    match = AVATAR_PATTERN.fullmatch(value)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de foto de perfil invalido.")
    try:
        image_bytes = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail="Foto de perfil invalida.")
    if len(image_bytes) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="A foto de perfil deve ter no maximo 400 KB.")
    return value


@auth_router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    try:
        username = normalize_username(payload.username)
    except ValueError:
        raise HTTPException(status_code=401, detail="Login ou senha invalidos.")

    db.query(AuthSession).filter(AuthSession.expires_at <= datetime.utcnow()).delete()
    user = db.query(AuthUser).filter(AuthUser.username == username).first()
    if not user or not verify_password(user, payload.password):
        raise HTTPException(status_code=401, detail="Login ou senha invalidos.")

    user.last_login = datetime.utcnow()
    token, auth_session = create_session(db, user)
    db.commit()
    db.refresh(user)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": auth_session.expires_at,
        "user": user_payload(user),
    }


@auth_router.post("/logout", status_code=204)
def logout(
    auth_session: AuthSession = Depends(get_auth_session),
    db: Session = Depends(get_db),
):
    db.delete(auth_session)
    db.commit()
    return Response(status_code=204)


@auth_router.get("/me")
def me(user: AuthUser = Depends(get_current_user)):
    return user_payload(user)


@auth_router.get("/users")
def list_users(
    _actor: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    users = db.query(AuthUser).order_by(AuthUser.is_owner.desc(), AuthUser.username).all()
    return {"items": [user_payload(user) for user in users]}


@auth_router.post("/users", status_code=201)
def create_user(
    payload: UserCreateRequest,
    actor: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        username = normalize_username(payload.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if db.query(AuthUser).filter(AuthUser.username == username).first():
        raise HTTPException(status_code=409, detail="Ja existe um usuario com esse login.")
    requested_role = _requested_role(payload)
    actor_role = user_role(actor)
    if requested_role == ROLE_DEVELOPER:
        raise HTTPException(status_code=403, detail="O perfil interno de desenvolvedor é reservado ao usuário principal. Use Administrador.")
    if actor_role == ROLE_GOVERNOR and requested_role != ROLE_VIEWER:
        raise HTTPException(status_code=403, detail="Governantes so podem criar Visualizadores.")

    user = AuthUser(
        username=username,
        is_owner=False,
        created_by_id=actor.id,
        avatar_data_url=_validated_avatar(payload.avatar_data_url),
    )
    apply_user_role(user, requested_role)
    try:
        set_password(user, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user_payload(user)


@auth_router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    actor: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = _find_user_or_404(db, user_id)
    _ensure_target_access(actor, target)

    role_changed = payload.role is not None or payload.is_admin is not None
    if role_changed:
        if user_role(actor) not in FULL_ACCESS_ROLES:
            raise HTTPException(status_code=403, detail="Somente administradores podem definir perfis.")
        requested_role = _requested_role(payload, user_role(target))
        if target.is_owner or requested_role == ROLE_DEVELOPER:
            raise HTTPException(status_code=400, detail="O perfil interno do usuário principal não pode ser atribuído ou alterado. Use Administrador para acesso completo.")
        apply_user_role(target, requested_role)

    password_changed = payload.password is not None
    if password_changed:
        try:
            set_password(target, payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.query(AuthSession).filter(AuthSession.user_id == target.id).delete()

    avatar_changed = "avatar_data_url" in payload.model_fields_set
    if avatar_changed:
        target.avatar_data_url = _validated_avatar(payload.avatar_data_url)

    if not password_changed and not role_changed and not avatar_changed:
        raise HTTPException(status_code=400, detail="Nenhuma alteracao foi informada.")

    target.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(target)
    return user_payload(target)


@auth_router.delete("/users/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    actor: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = _find_user_or_404(db, user_id)
    _ensure_target_access(actor, target)
    if target.is_owner:
        raise HTTPException(status_code=400, detail="DEUSM2 nao pode ser removido.")
    if target.id == actor.id:
        raise HTTPException(status_code=400, detail="Voce nao pode remover a propria conta.")

    db.query(AuthSession).filter(AuthSession.user_id == target.id).delete()
    db.query(AuthUser).filter(AuthUser.created_by_id == target.id).update(
        {AuthUser.created_by_id: None},
        synchronize_session=False,
    )
    db.delete(target)
    db.commit()
    return Response(status_code=204)
