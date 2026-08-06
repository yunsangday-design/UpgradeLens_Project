from pydantic import BaseModel, validator


class Order(BaseModel):
    sku: str

    @validator("sku")
    def sku_upper(cls, value):
        return value.upper()
