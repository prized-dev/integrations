from typing import Literal

from pydantic import BaseModel, ConfigDict


class CustomRuntimeConfig(BaseModel):
    # pydantic_config.BaseConfig: extra fields forbidden.
    model_config = ConfigDict(extra="forbid")

    type: Literal["custom"] = "custom"
    import_path: str
