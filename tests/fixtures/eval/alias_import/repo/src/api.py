import pydantic as pyd


class Config(pyd.BaseSettings):
    host: str = "localhost"
    port: int = 8080


class Payload(pyd.BaseModel):
    body: str

    def as_dict(self):
        return self.dict()
