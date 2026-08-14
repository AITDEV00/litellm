"""Identity facts for a discovered model. Canonical domain: no runtime imports."""

from pydantic import BaseModel


class ModelIdentity(BaseModel):
    logical_model_name: str
    upstream_model_id: str | None = None

    root: str | None = None
    parent: str | None = None

    display_name: str | None = None
    canonical_id: str | None = None
    hugging_face_id: str | None = None
    created: int | None = None