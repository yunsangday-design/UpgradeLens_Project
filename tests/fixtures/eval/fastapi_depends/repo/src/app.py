from fastapi import Depends, FastAPI

app = FastAPI()


def get_db() -> object:
    return None


@app.get("/users")
def list_users(db: object = Depends(get_db)) -> list:
    return []
