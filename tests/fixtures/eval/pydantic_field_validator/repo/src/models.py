from pydantic import BaseModel, BaseSettings, validator


class Settings(BaseSettings):
    api_key: str


class User(BaseModel):
    name: str

    @validator("name")
    def _check_name(cls, value: str) -> str:
        if not value:
            raise ValueError("name is required")
        return value
