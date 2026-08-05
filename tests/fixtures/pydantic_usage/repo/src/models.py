from pydantic import BaseModel, validator, root_validator


class User(BaseModel):
    name: str
    age: int

    class Config:
        allow_population_by_field_name = True

    @validator("name")
    def check_name(cls, v):
        return v

    @root_validator
    def check_all(cls, values):
        return values
