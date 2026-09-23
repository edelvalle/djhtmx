# Lazy model fields

`ModelConfig(lazy=True)` holds the pk and reads the row only when something touches it, so a row that most requests never look at costs nothing to rebuild:

```python
opened: Annotated[Item | None, ModelConfig(lazy=True)] = None
```

The field then holds a proxy, and the query happens at the first attribute access.  Everything else behaves as the eager field does: equality, the state round-trip, the `None` for a row that was deleted, and `select_related`/`prefetch_related`, which are applied to that deferred read.

It pays for a row that one branch of the render or one handler reads:

```python
class ItemEditor(HtmxComponent):
    owner: Owner
    opened: Annotated[Item | None, ModelConfig(lazy=True)] = None

    @property
    def items(self) -> ItemQS:
        return Item.objects.filter(owner=self.owner)
```

Here most requests list the items and never look at `opened`, so most requests never read that row.

It pays for nothing when the template always shows the row -- there the query happens anyway, only later and out of sight, which makes a slow render harder to read.  Leave that field eager.

Two lazinesses live next to each other and are unrelated: this one defers *reading a row*, while [lazy-components](lazy-components.md) defers *rendering a whole component*.  A field can be lazy in a component that is not, and the other way round.
