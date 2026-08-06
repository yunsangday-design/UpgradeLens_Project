from pydantic import BaseModel


class Sample(BaseModel):
    value: int


def test_sample():
    assert Sample(value=1).dict() == {"value": 1}
