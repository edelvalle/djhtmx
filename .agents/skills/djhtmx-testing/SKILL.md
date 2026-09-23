---
name: djhtmx-testing
description: "Read this before writing or changing a test of an HtmxComponent: how to drive a component with djhtmx.testing.Htmx, what goes stale between sends, and how to assert on what never reaches the DOM."
---

# Testing djhtmx components

`djhtmx.testing.Htmx` wraps a Django test client and runs the same dispatch the browser does: it navigates to a real page, keeps the rendered DOM, and applies everything a handler produces -- renders, out-of-band swaps, destroys, URL changes, pending SSE events.

Read the `djhtmx-components` skill for the component under test.

## Two rules

**Drive the real page.**  A component built by hand -- constructed directly, with `model_construct`, or through a factory of your own -- has no repository, no session and no rendered DOM.  The test then passes over the placement, the state round-trip and the render, which is where the bugs are.

**Never call a handler yourself.**  `editor.toggle_item(item)` skips the command processor: the arguments are never coerced, the commands are never processed, the state is never stored, and nothing renders.  Send it instead.

## A test

```python
class TestTheItemEditor(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = OwnerFactory.create()
        cls.first = ItemFactory.create(owner=cls.owner, name="First item")
        cls.second = ItemFactory.create(owner=cls.owner, name="Second item")

    def setUp(self):
        client = Client()
        client.force_login(UserFactory.create(is_staff=True))
        self.htmx = Htmx(client)
        self.htmx.navigate_to(reverse("owner-detail", args=[self.owner.id]))

    def test_choosing_an_item_marks_it(self):
        editor = self.htmx.get_component_by_type(ItemEditor)
        self.htmx.send(editor.open_the_item, item=self.first)
        self.htmx.send(editor.toggle_item, item=self.second)
        # `editor` is stale -- read the component again
        editor = self.htmx.get_component_by_type(ItemEditor)
        chosen = [row.name for row in editor.items if row.chosen]
        self.assertEqual(chosen, ["Second item"])

    def test_it_asks_for_an_open_item_first(self):
        editor = self.htmx.get_component_by_type(ItemEditor)
        with self.htmx.assertEmits(ItemsRebuildRefused) as captured:
            self.htmx.send(editor.rebuild_the_items)
        self.assertEqual(captured.get_event().reason, "no-open-item")
```

## Reference

Read the file for what the test is about to do:

- [basic](references/basic.md) -- building the client, navigating, finding components, logging in, the reference that goes stale, and the placeholder a lazy component renders first.
- [interactions](references/interactions.md) -- `send` through the bound method, `type_into` and `trigger` for going through the template, `dispatch_event` as the escape hatch.
- [assertions](references/assertions.md) -- `assertEmits`, `assertYields`, `capturing`, and why the assertions go after the block.
- [the-dom](references/the-dom.md) -- `select`, `find_by_text`, `print`, the lxml truthiness trap, and what to assert on the DOM rather than on the component.
- [urls](references/urls.md) -- starting a component from the query string, asserting that a handler moved the URL, and what a `Redirect` does to a test.
- [sse](references/sse.md) -- `drain_sse_events`, emitting from a test, and telling "from this page" apart from "from elsewhere".
