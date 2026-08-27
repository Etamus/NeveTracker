from datetime import datetime, timedelta
import hashlib
import hmac
import re
import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models.auth import AuthSession, AuthUser


PASSWORD_ITERATIONS = 310_000
SESSION_DURATION = timedelta(days=7)
USERNAME_PATTERN = re.compile(r"^[A-Z0-9._-]{3,40}$")


def normalize_username(value):
    username = (value or "").strip().upper()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Use de 3 a 40 letras, numeros, ponto, hifen ou sublinhado.")
    return username


def set_password(user, password):
    if not isinstance(password, str) or len(password) < 4 or len(password) > 200:
        raise ValueError("A senha deve ter entre 4 e 200 caracteres.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    user.password_salt = salt.hex()
    user.password_hash = digest.hex()


def verify_password(user, password):
    try:
        salt = bytes.fromhex(user.password_salt)
        expected = bytes.fromhex(user.password_hash)
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return hmac.compare_digest(actual, expected)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db, user):
    token = secrets.token_urlsafe(48)
    session = AuthSession(
        token_hash=hash_token(token),
        user_id=user.id,
        expires_at=datetime.utcnow() + SESSION_DURATION,
    )
    db.add(session)
    return token, session


def user_payload(user):
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": bool(user.is_admin),
        "is_owner": bool(user.is_owner),
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "last_login": user.last_login,
    }


def get_bearer_token(authorization: str = Header(default="")):
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Sessao nao informada.")
    return token.strip()


def get_auth_session(
    token: str = Depends(get_bearer_token),
    db: Session = Depends(get_db),
):
    auth_session = (
        db.query(AuthSession)
        .filter(AuthSession.token_hash == hash_token(token))
        .first()
    )
    if not auth_session or auth_session.expires_at <= datetime.utcnow():
        if auth_session:
            db.delete(auth_session)
            db.commit()
        raise HTTPException(status_code=401, detail="Sessao invalida ou expirada.")
    return auth_session


def get_current_user(auth_session: AuthSession = Depends(get_auth_session)):
    if not auth_session.user:
        raise HTTPException(status_code=401, detail="Usuario nao encontrado.")
    return auth_session.user


def require_admin(user: AuthUser = Depends(get_current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores.")
    return user


def ensure_owner_user(db):
    owner = db.query(AuthUser).filter(AuthUser.username == "DEUSM2").first()
    if not owner:
        owner = AuthUser(username="DEUSM2", is_admin=True, is_owner=True)
        set_password(owner, "1234")
        db.add(owner)
    else:
        owner.is_admin = True
        owner.is_owner = True
    db.commit()
    return owner
