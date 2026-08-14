"""Token limits for a discovered model. Canonical domain: no runtime imports."""

from pydantic import BaseModel


class ModelLimits(BaseModel):
    context_length: int | None = None
    max_input_tokens: int | None = None
    max_completion_tokens: int | None = None