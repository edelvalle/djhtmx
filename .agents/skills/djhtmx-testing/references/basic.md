# Driving a page from a test

`Htmx` wraps a Django test client and runs the same dispatch the browser does.  A test builds it, navigates to a real page, and then works with the components that page placed:

```python
class TestTheItemEditor(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = OwnerFactory.create()

    def setUp(self):
        self.htmx = Htmx(Client())
        self.htmx.navigate_to(reverse("owner-detail", args=[self.owner.id]))

    def test_it_opens_an_item(self):
        editor = self.htmx.get_component_by_type(ItemEditor)
        ...
```

`navigate_to` follows redirects, asserts the page answered, parses the HTML into `htmx.dom`, and builds the repository the components live in.  It also keeps `htmx.path` and `htmx.query_string` -- see [urls](urls.md).

## Never build a component by hand

A component constructed directly, with `model_construct`, or through a factory of your own has no repository, no session and no rendered DOM.  A test built that way passes over the placement, the state round-trip and the render, which is where the bugs are.  Go through a page that places the component, even when the page is a fixture template that exists only for the test.

Find it with:

- `get_component_by_type(ItemEditor)` -- the one instance on the page; it fails when there are several.
- `get_components_by_type(TodoItem)` -- all of them, for a page that repeats one component per row.
- `get_component_by_id(component_id)` -- when the test already knows the id, typically one it read off the DOM.

## A component reference goes stale, its id does not

djhtmx rebuilds the component from the session on every request, so the object a test holds is a snapshot.  Sending through it is fine -- `send` uses only its id and the handler's name -- but reading it after a send answers with the old state:

```python
self.htmx.send(editor.toggle_item, item=self.second)
editor = self.htmx.get_component_by_type(ItemEditor)  # the one above is stale
self.assertEqual([row.name for row in editor.items if row.chosen], ["Second item"])
```

One reference drives a whole test; read the component again before every assertion about its state.  `htmx.dom` is not stale -- every send applies what it produced.

## Logging in

Log the client in before building `Htmx`, or before navigating:

```python
client = Client()
client.force_login(UserFactory.create(is_staff=True))
self.htmx = Htmx(client)
```

The user that reaches the components is the one the page rendered with, so a test that forgets this drives an anonymous page: components annotated `user: User | None` get `None`, and the ones that require a login are never built at all.

A component that requires a login answers an anonymous visitor with a redirect to the login page, not with a 500.  Test that with the plain Django client and `assertRedirects`, not through `Htmx`: `navigate_to` follows the redirect and then finds no djhtmx session on the login page, which fails with a confusing assertion instead of the one you meant.

`requires_logged_user(SomeComponent)` says which of the two a component is, and is the assertion to make when a base class is supposed to carry the requirement for a family of components.

## A lazily placed component renders a placeholder first

A component placed with `lazy=True` puts its placeholder in the DOM; the real render is what the browser fetches when the element is revealed.  The component is in the session all the same, so `get_component_by_type` finds it, but `htmx.dom` holds the placeholder.  Drive its `render` handler to get the real one:

```python
report = self.htmx.get_component_by_type(ExpensiveReport)
self.htmx.send(report.render)
```
