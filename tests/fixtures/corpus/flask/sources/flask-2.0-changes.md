# Flask 2.0 changes

Flask 2.0 drops Python 2 support, removes long-deprecated re-exports and
changes how JSON serialisation is configured.

## flask.Markup and flask.escape are removed

`flask.Markup` and `flask.escape` were re-exports of MarkupSafe kept for
backwards compatibility. They are deprecated in Flask 2.0 and removed in 2.3.

Import them from MarkupSafe instead:

```python
from markupsafe import Markup, escape
```

Any code doing `from flask import Markup` or `flask.escape(value)` must be
updated; there is no shim.

## flask.json.JSONEncoder is replaced by app.json

Subclassing `flask.json.JSONEncoder` / `flask.json.JSONDecoder` and assigning
`app.json_encoder` or `app.json_decoder` is deprecated. Serialisation is now
controlled by a JSON provider on the application.

```python
from flask.json.provider import DefaultJSONProvider

class MyProvider(DefaultJSONProvider):
    def default(self, o):
        ...

app.json = MyProvider(app)
```

`flask.json.dumps` and `flask.json.loads` keep working but now delegate to the
active provider, so they require an application context to pick up app config.

## before_first_request is deprecated

`@app.before_first_request` is deprecated because it makes startup ordering
depend on the first incoming request. Run the setup code at import time, or
register it explicitly during application factory construction.

## Async views require the async extra

Flask 2.0 can dispatch `async def` view functions, but only when installed as
`pip install "flask[async]"`. Without the extra, an async view raises at
request time rather than at import time.
