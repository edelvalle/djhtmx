### Code Organization

When writing functions, classes, constants, etc., put public, high-level entry points first, followed by lower-level helpers and private utilities. This lets developers read files like prose, moving from general concepts to
specific details.

Good:

```python
def process_report(data: bytes) -> str:
    cleaned = _clean_data(data)
    return _format_report(cleaned)

def _clean_data(data: bytes) -> bytes:
    ...

def _format_report(data: bytes) -> str:
    ...
```

Bad:

```python
# Bad - helpers before the public API makes the file read backwards
def _clean_data(data: bytes) -> bytes:
    ...

def process_report(data: bytes) -> str:
    cleaned = _clean_data(data)
    return _format_report(cleaned)
```

The exception to this rule is when the function is actually used during module initialization in later declarations.  For example, as the input of some field definition in a model.

**Locality:** when a function is only used by (called from) a single class and is not reused anywhere else, define it *inside* that class — as a method, or a `@staticmethod` if it needs no instance state — rather than as a module-level private function. Keeping it next to its only caller improves locality and makes the class self-contained.

```python
# Good - the only caller is CommandProcessor, so the helper lives in it
class CommandProcessor:
    async def _arun_command(self, ...):
        ...
        emitted = await self._acollect(handler, **kwargs)
        ...

    @staticmethod
    async def _acollect(handler, /, *args, **kwargs) -> list:
        ...

# Bad - a module-level private helper used by exactly one class
async def _acollect(handler, /, *args, **kwargs) -> list:
    ...

class CommandProcessor:
    ...
```

### Python Style

**Branching:**

When a function dispatches on several mutually-exclusive conditions and returns from each, prefer a single `if / elif / … / else` chain over a sequence of standalone `if …: return` statements. The chain reads as one decision and makes the branches' mutual exclusivity explicit. (Ruff's `RET` rules are intentionally not enabled, so `elif`/`else` after a `return` is fine.)

```python
# Good - one decision, branches are obviously mutually exclusive
if isasyncgenfunction(handler):
    return async_to_sync(_drain_async_handler)(handler, args, kwargs)
elif iscoroutinefunction(handler):
    return async_to_sync(_await_handler)(handler, args, kwargs)
else:
    return _drain_sync_handler(handler, args, kwargs)

# Bad - reads as three independent checks
if isasyncgenfunction(handler):
    return async_to_sync(_drain_async_handler)(handler, args, kwargs)
if iscoroutinefunction(handler):
    return async_to_sync(_await_handler)(handler, args, kwargs)
return _drain_sync_handler(handler, args, kwargs)
```


**Pattern Matching:**

Use `match...case` for multiple cases with `assert_never()` for exhaustiveness. NEVER use underscore (`_`) as a catch-all - explicitly handle each case to ensure type safety and catch errors when new cases are added.

Good:

```python
from typing import Literal, assert_never

def process_value(value: Literal['a', 'b', 'c']):
    match value:
        case 'a': return 'Alpha'
        case 'b': return 'Beta'
        case 'c': return 'Gamma'
        case unreachable: assert_never(unreachable)

# Bad - using underscore hides errors when new cases are added
def process_value_bad(value: Literal['a', 'b', 'c']):
    match value:
        case 'a': return 'Alpha'
        case 'b': return 'Beta'
        case _: return 'Unknown'  # Wrong! If 'c' is added to the type, this silently catches it
```


Avoid `isinstance` checks inside match cases. Instead, use nested patterns, value matching, or match over tuples.

When you find yourself using `isinstance()` or attribute comparisons inside a match case, it usually means you should refactor to use Python's pattern matching features more fully.

Good:

```python
# Good - nested pattern matching with value matching
match self.block:
    case OKNotOKInspectionBlock(status=BooleanAnswered(value=value)):
        return None
    case OKNotOKInspectionBlock(status=prev_status):
        return create_event(prev_status)


# ❌ Bad - isinstance inside match case
match self.block:
    case OKNotOKInspectionBlock(status=prev_status):
        if isinstance(prev_status, BooleanAnswered) and prev_status.value == value:
            return None
        return create_event(prev_status)
```

Also

```python
# ❌ Bad - complex conditions inside case
match block:
    case SomeBlock(item=item):
        if isinstance(item, Product) and item.stock > 0 and item.category == "electronics":
            return process_electronics(item)

# ✅ Good - nested pattern with guard
match block:
    case SomeBlock(item=Product(stock=stock, category="electronics")) if stock > 0:
        return process_electronics(block.item)

# ✅ Also good - match over tuple for orthogonal conditions
match (has_inventory, block):
    case (True, SomeBlock(item=Product(category="electronics"))):
        return process_electronics(block.item)
```

**Type Hints:**

- Avoid `typing.Any` unless necessary

- Use native types for concrete type hints: `dict`, `list`, `tuple`, `X | None` (not `Dict`, `List`, `Optional`, `Tuple`); but prefer the most generic type for arguments.

  ```python
  def custom_map[P, R](fn: Callable[P, R], items: Iterable[P]) -> list[R]:
      return [fn(item) for item in items]
  ```

- Avoid `TypeVar` when possible and use type parameter syntax.

**Class Attributes:**

Use immutable values for class attributes. Do not assign mutable containers (`list`, `dict`,
`set`) as class-level defaults unless the class intentionally owns shared mutable state and
documents why. Prefer immutable containers such as tuples or `frozenset`, or create per-instance
values with `field(default_factory=...)` or in `__init__`.
