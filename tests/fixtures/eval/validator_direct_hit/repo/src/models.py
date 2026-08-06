from pydantic import BaseModel, validator


class User(BaseModel):
    name: str
    age: int

    @validator("name")
    def name_must_not_be_blank(cls, value):
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


def serialise(user: User) -> dict:
    return user.dict()
