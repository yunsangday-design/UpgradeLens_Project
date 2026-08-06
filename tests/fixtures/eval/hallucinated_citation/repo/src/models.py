from pydantic import BaseModel, validator


class Account(BaseModel):
    owner: str

    @validator("owner")
    def normalise_owner(cls, value):
        return value.strip()
