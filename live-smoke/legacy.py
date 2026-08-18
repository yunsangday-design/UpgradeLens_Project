"""Legacy helpers removed by this PR (breaking change fixture)."""


def old_endpoint(payload: dict) -> dict:
    return {"legacy": True, "payload": payload}


def deprecated_hook(name: str) -> str:
    return f"hook:{name}"
