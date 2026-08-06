import json
import os
from dataclasses import dataclass


@dataclass
class Settings:
    host: str = "localhost"
    port: int = 8080


def load(path: str) -> Settings:
    if not os.path.exists(path):
        return Settings()
    with open(path, encoding="utf-8") as fh:
        return Settings(**json.load(fh))
