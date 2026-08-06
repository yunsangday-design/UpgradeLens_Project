from pydantic import BaseModel, validator


class Invoice(BaseModel):
    number: str

    @validator("number")
    def normalise(cls, value):
        return value.strip()
