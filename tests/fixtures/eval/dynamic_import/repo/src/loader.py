import importlib


def load_model_class(name: str):
    module = importlib.import_module("pydantic")
    return getattr(module, name)


def build(name: str, **kwargs):
    return load_model_class(name)(**kwargs)
