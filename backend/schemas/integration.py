from typing import Literal, Optional

from pydantic import BaseModel, Field


class IntegrationUpdateRequest(BaseModel):
    enabled: bool = False
    api_key: Optional[str] = Field(default=None, max_length=1000)
    clear_api_key: bool = False
    usage_mode: Literal["fallback", "always"] = "fallback"
    max_results: int = Field(default=20, ge=10, le=100)
    zone: Optional[str] = Field(default=None, max_length=120)
    actor_id: Optional[str] = Field(default=None, max_length=200)
    login: Optional[str] = Field(default=None, max_length=200)
