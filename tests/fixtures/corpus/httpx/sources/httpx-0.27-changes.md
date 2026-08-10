# httpx 0.27 changelog

Release 0.27 deprecates several client arguments ahead of the 1.0 API freeze.
Deprecated arguments still work but emit `DeprecationWarning`.

## The proxies argument is deprecated in favour of proxy

`httpx.Client(proxies=...)` and the module-level helpers accepted a mapping of
URL patterns to proxies. Use the singular `proxy` argument for the common case,
or `mounts` when different patterns need different transports.

```python
client = httpx.Client(proxy="http://localhost:8030")
client = httpx.Client(mounts={"http://": httpx.HTTPTransport(proxy=...)})
```

## The app argument is deprecated in favour of ASGITransport

Passing an ASGI application with `httpx.Client(app=app)` is deprecated. Build
the transport explicitly so the client no longer special-cases ASGI:

```python
transport = httpx.ASGITransport(app=app)
client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
```

The same applies to WSGI applications, which now use `httpx.WSGITransport`.

## Passing raw bytes to data is deprecated

`data=<str|bytes>` was overloaded: it meant "form fields" for a dict and "raw
body" for a string. Raw bodies must use `content=` instead.

```python
httpx.post(url, content=b"raw-body")
httpx.post(url, data={"field": "value"})
```
