from typing import Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=4, max_length=200)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=4, max_length=200)
    is_admin: bool = False


class UserUpdateRequest(BaseModel):
    password: Optional[str] = Field(default=None, min_length=4, max_length=200)
    is_admin: Optional[bool] = None
