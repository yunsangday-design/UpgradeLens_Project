from pydantic import BaseModel, Field


class User(BaseModel):
    full_name: str = Field(..., alias="fullName")
