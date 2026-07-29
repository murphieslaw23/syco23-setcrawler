from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


ImageKind = Literal["flyer", "artist", "crew", "label", "thumbnail"]


class SetImage(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    remote_url: str | None = None
    storage_path: str | None = None
    web_variant_path: str | None = None
    kind: ImageKind = "thumbnail"
    width: int | None = None
    height: int | None = None
    perceptual_hash: str | None = None
    attribution: str | None = None
    is_primary: bool = False
    priority: int = 0
