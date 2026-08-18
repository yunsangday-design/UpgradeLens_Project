"""Sample application module used by capability gold-set evaluations.

The capability analyzers in ``fake`` mode return canned reports whose evidence
cites ``src/app.py`` (lines 12 / 30 / 42). Keeping this file present lets the
``issue_repair`` verifier confirm the proposed patch targets a real file, so the
gold cases can assert ``verification_passed == True``.
"""


def handle(req):
    user = req.user
    if user is None:
        return None
    value = process(user)
    return value


def process(user):
    return user.name


def old_helper():
    return "legacy"


def model_dump(data):
    return dict(data)


CONFIG = {"enabled": True}
