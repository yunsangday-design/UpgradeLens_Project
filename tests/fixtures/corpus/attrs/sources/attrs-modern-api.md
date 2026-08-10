# attrs modern API migration

The `attrs` package exposes two import namespaces. The old `attr` namespace is
kept forever for backwards compatibility, but new code should use `attrs`.

## attr.s and attr.ib are superseded by attrs.define and attrs.field

`@attr.s` / `attr.ib()` keep the historic defaults. `@attrs.define` and
`attrs.field()` are the modern equivalents with better defaults:

```python
import attrs

@attrs.define
class Point:
    x: int
    y: int = attrs.field(default=0)
```

`attrs.define` implies `slots=True`, `auto_attribs=True`, `kw_only=False` and
keyword-only `__init__` validation, so subclasses that set attributes after
construction may need `@attrs.define(slots=False)`.

## The cmp argument was removed in favour of eq and order

`attr.s(cmp=True)` and `attr.ib(cmp=...)` were removed. Comparison behaviour is
now split into two independent options:

```python
@attrs.define(eq=True, order=False)
class Config:
    name: str
```

`eq` controls `__eq__`/`__ne__`; `order` controls `__lt__` and friends. Code
passing `cmp` raises `TypeError`.

## attr.asdict and attr.astuple moved to the attrs namespace

`attr.asdict`, `attr.astuple`, `attr.fields` and `attr.evolve` are available as
`attrs.asdict`, `attrs.astuple`, `attrs.fields` and `attrs.evolve`. The old
names still work; mixing namespaces in one module is what causes confusion.
