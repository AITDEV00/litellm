"""Architecture facts for a discovered model. Canonical domain: no runtime imports."""

from pydantic import BaseModel, Field


class ModelArchitecture(BaseModel):
    model_type: str | None = None
    architectures: list[str] = Field(default_factory=list)
    tokenizer: str | None = None
    instruct_type: str | None = None