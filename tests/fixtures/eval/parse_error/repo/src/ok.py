from pydantic import BaseModel, validator


class Item(BaseModel):
    code: str

    @validator("code")
    def strip_code(cls, value):
        return value.strip()
