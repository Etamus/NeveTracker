from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from database import get_db
from models.auth import AuthSession, AuthUser
from schemas.auth import LoginRequest, UserCreateRequest, UserUpdateRequest
from services.auth_service import (
    create_session,
    get_auth_session,
    get_current_user,
    normalize_username,
    require_admin,
    set_password,
    user_payload,
    verify_password,
)


auth_router = APIRouter()


def _find_user_or_404(db, user_id):
    user = db.query(AuthUser).filter(AuthUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")
    return user


def _ensure_target_access(actor, target):
    if target.is_owner and not actor.is_owner:
        raise HTTPException(status_code=403, detail="O usuario principal e protegido.")
    if target.is_admin and not actor.is_owner and target.id != actor.id:
        raise HTTPException(status_code=403, detail="Somente DEUSM2 pode alterar outro administrador.")


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
    if payload.is_admin and not actor.is_owner:
        raise HTTPException(status_code=403, detail="Somente DEUSM2 pode definir administradores.")

    user = AuthUser(
        username=username,
        is_admin=payload.is_admin,
        is_owner=False,
        created_by_id=actor.id,
    )
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

    if payload.is_admin is not None:
        if not actor.is_owner:
            raise HTTPException(status_code=403, detail="Somente DEUSM2 pode definir administradores.")
        if target.is_owner and payload.is_admin is False:
            raise HTTPException(status_code=400, detail="DEUSM2 deve permanecer administrador.")
        target.is_admin = payload.is_admin

    password_changed = payload.password is not None
    if password_changed:
        try:
            set_password(target, payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.query(AuthSession).filter(AuthSession.user_id == target.id).delete()

    if not password_changed and payload.is_admin is None:
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
