from typing import Literal, Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=4, max_length=200)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=4, max_length=200)
    role: Optional[Literal["developer", "governor", "viewer"]] = None
    is_admin: Optional[bool] = None
    avatar_data_url: Optional[str] = Field(default=None, max_length=500_000)


class UserUpdateRequest(BaseModel):
    password: Optional[str] = Field(default=None, min_length=4, max_length=200)
    role: Optional[Literal["developer", "governor", "viewer"]] = None
    is_admin: Optional[bool] = None
    avatar_data_url: Optional[str] = Field(default=None, max_length=500_000)
