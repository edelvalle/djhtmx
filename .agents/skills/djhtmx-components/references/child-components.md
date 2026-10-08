# Building and destroying child components

When a handler adds one row to a list, re-rendering the parent redraws every row to show one new one.  `BuildAndRender` builds the child component and places it, leaving everything already on the page alone:

```python
class ListHeader(HtmxComponent):
    _template_name = "todo/ListHeader.html"

    def add(self, new_item: str):
        item = Item.objects.create(text=new_item)
        yield BuildAndRender.append(
            "#todo-list",
            TodoItem,
            id=f"item-id-{item.id.hex}",
            parent_id=self.id,
            item=item,
        )
```

The first argument is the CSS selector of the container, the second the component class, and the rest is the child's initial state.  `append` and `prepend` put it inside the target, `after` and `before` beside it, and `update` replaces a component that is already on the page by its id.

Give the child a stable `id` derived from the row it shows.  That is what lets a later `update` or `Destroy` find it, and what keeps two renders of the same row from becoming two components.

## Pair it with SkipRender when the parent draws the same thing

A parent whose own render lists the rows would draw the new child a second time, and the appended fragment would arrive after that render -- so the row appears twice:

```python
yield SkipRender(self)
yield BuildAndRender.append("#todo-list", TodoItem, id=..., parent_id=self.id, item=item)
```

Let the parent render instead of skipping when its own body depends on the change -- a count, an empty-state, a footer that appears with the first row.  Then re-rendering the parent is the cheaper correct answer and `BuildAndRender` is not needed at all.

## parent_id, or the state outlives the DOM

`parent_id` registers the new component as a child of that one.  `Destroy` on the parent then reaches it:

```python
def delete(self):
    if self.item:
        self.item.delete()
    yield Destroy(self.id)
```

Without `parent_id`, destroying the parent leaves the child's state in the session: the DOM node goes away with its parent, and the entry that describes it stays in Redis until the session expires.  A page that builds children in a loop and drops them leaks a little on every cycle.

Components placed with `{% htmx %}` inside another component's template are tracked automatically -- the tag reads the surrounding component from the template context.  `parent_id` is for the ones a handler builds.

`Destroy` takes the djhtmx component id, not a DOM id, and it is the component's own `self.id` in nearly every case: a component destroys itself when the thing it stood for is gone.
