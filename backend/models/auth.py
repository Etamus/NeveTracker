from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from database import Base


class AuthUser(Base):
    __tablename__ = "auth_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(40), unique=True, index=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    password_salt = Column(String(64), nullable=False)
    role = Column(String(20), default="viewer", nullable=False)
    logistics_postal_code = Column(String(8), nullable=False, default='01001000')
    avatar_data_url = Column(Text, nullable=True)
    is_admin = Column(Boolean, default=False, nullable=False)
    is_owner = Column(Boolean, default=False, nullable=False)
    created_by_id = Column(
        Integer,
        ForeignKey("auth_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_login = Column(DateTime, nullable=True)

    sessions = relationship(
        "AuthSession",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("auth_users.id"), index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)

    user = relationship("AuthUser", back_populates="sessions")
