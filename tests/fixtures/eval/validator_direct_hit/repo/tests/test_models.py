from src.models import User, serialise


def test_serialise():
    assert serialise(User(name="ada", age=36))["name"] == "ada"
