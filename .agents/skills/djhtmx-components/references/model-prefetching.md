# Prefetching the rows a render walks

djhtmx rebuilds the component on every request, so every relation the render reaches *through* a field is read again.  The ones that cost are to-many: a template that loops over `item.attachments.all` and reads `attachment.uploader` for each one pays a query per attachment, and one that asks for `item.attachments.all` twice -- once in an `{% if %}` and again in the `{% for %}` -- pays for each ask, because that call builds a fresh queryset every time.

`ModelConfig(prefetch_related=...)` answers all of them with one query per lookup, whatever the row count.  This is where the gains are:

```python
item: Annotated[Item, ModelConfig(prefetch_related=("attachments__uploader",))]
```

Prefetch the whole path the render walks, not its first step: `("attachments",)` alone still leaves a query per attachment for the uploader.  Pass a `Prefetch` object where the prefetch needs a queryset of its own, to filter or order it:

```python
item: Annotated[
    Item,
    ModelConfig(prefetch_related=(Prefetch("attachments", Attachment.objects.order_by("-created_at")),)),
]
```

`select_related` is the smaller tool.  It folds a to-one relation into the row's own query, so it saves the one query that reading `item.owner` would have cost -- one, not one per row -- and pays for it with a join and a wider row on every request, whether the render reads the owner or not.  Worth it when it nearly always does:

```python
item: Annotated[Item, ModelConfig(select_related=("owner",))]
```

## Sharing a row between components

Each component reads its own rows, so the components of one request that hold the same row -- a list and the editor of its open item, or every subscriber one `Emit` wakes -- read it once each and get an instance each.  `ModelConfig(cache=True)` makes every field annotated with it read the row once per request or SSE wakeup, and share that instance:

```python
item: Annotated[Item, ModelConfig(cache=True, prefetch_related=("attachments__uploader",))]
```

A field asking for relations the shared instance lacks gets them loaded onto it, so every field gets its `select_related` and `prefetch_related`, and the relations already loaded are not read again.  A field whose `prefetch_related` holds a `Prefetch` object never shares the model: it reads its row on its own, because the queryset of its `Prefetch` would reach every other field holding the instance.

A shared instance is one object: a component that changes it changes it for every component holding that row in the same request.

A handler that deletes a shared row, or writes to it without going through the shared instance -- `Item.objects.filter(...).update(...)`, another instance of the row -- yields `InvalidateModelCache(item)`, or `InvalidateModelCache((Item, item_id))`, so the components built after it read the row again.  Changing the field's instance and calling `save()` needs nothing: the shared instance already holds the change.  djhtmx does not invalidate on its own, and without it the components get the deleted instance, or the one from before the write.  The components already built keep the instance they hold.  The model of `(Model, pk)` is the one the field is annotated with, so a field annotated with a multi-table child is dropped through the child.

`DJHTMX_DEFAULT_MODEL_CACHE = True` turns the cache on for every model field that leaves `cache` unset, and `cache=False` opts one field out.  A row passed as an instance when placing the component never goes through the cache: it is already in hand.

## ModelConfig reaches the state, and nothing else

A queryset a property builds is a separate read, so it prefetches for itself:

```python
@property
def items(self) -> ItemQS:
    return Item.objects.filter(owner=self.owner).prefetch_related("attachments")
```

The same goes for a `QuerySet` field: it is stored as a list of pks and read back with a plain `filter(pk__in=...)`, so what the render needs off those rows is prefetched by the property that uses them, not by the annotation.

## Measuring instead of guessing

Both are annotations you add because a render is slow, not because a relation exists.  Count the queries of the page before and after -- `assertNumQueries` around a `navigate_to`, or the query log -- and keep the annotation that changed the number.  A prefetch nothing reads is a second query for rows nobody looks at.
