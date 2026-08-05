import pydantic
import pydantic as pyd

MyModel = pyd.BaseModel
Field = pyd.Field

x = pydantic.VERSION


class Settings(pydantic.BaseSettings):
    pass
