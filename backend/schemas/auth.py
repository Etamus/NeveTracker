from typing import Literal, Optional
import re

from pydantic import BaseModel, Field
from pydantic import field_validator


class UserPreferencesRequest(BaseModel):
    logistics_postal_code: str = '01001000'

    @field_validator('logistics_postal_code')
    @classmethod
    def validate_postal_code(cls, value):
        value = value.strip()
        if not value:
            return '01001000'
        if not re.fullmatch(r'\d{5}-?\d{3}', value):
            raise ValueError('Informe um CEP com oito dígitos.')
        return value.replace('-', '')


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=4, max_length=200)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=4, max_length=200)
    role: Optional[Literal["developer", "administrator", "governor", "viewer"]] = None
    is_admin: Optional[bool] = None
    avatar_data_url: Optional[str] = Field(default=None, max_length=500_000)


class UserUpdateRequest(BaseModel):
    password: Optional[str] = Field(default=None, min_length=4, max_length=200)
    role: Optional[Literal["developer", "administrator", "governor", "viewer"]] = None
    is_admin: Optional[bool] = None
    avatar_data_url: Optional[str] = Field(default=None, max_length=500_000)
