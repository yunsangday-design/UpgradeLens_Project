"""Demo service module with deliberate configuration bugs (B2 gold set)."""

import json


def get_config(mapping, key):
    """BUG(configuration): missing key raises KeyError instead of a default."""
    return mapping[key]


def get_port(env_name):
    """BUG(configuration): env value stays a string instead of an int."""
    import os

    return os.environ.get(env_name, "8000")


def load_settings(raw):
    """BUG(configuration): invalid JSON raises JSONDecodeError instead of returning {}."""
    return json.loads(raw)
