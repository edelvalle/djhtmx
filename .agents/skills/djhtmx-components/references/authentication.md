All components have a `user: AbstractBaseUser | None` representing the current logged in user or `None` in case the user is anonymous. To enforce the component requires a logged-in user, annotate the field with your user model -- usually once, in a base component:

```python
from typing import Annotated
from pydantic import Field
from djhtmx.component import HtmxComponent

class BaseComponent(HtmxComponent, public=False):
    user: Annotated[User, Field(exclude=True)]


class Counter(BaseComponent):
    _template_name = "Counter.html"
    counter: int = 0

    def inc(self, amount: int = 1):
        self.counter += amount
```

That annotation *is* the login requirement, and djhtmx enforces it: building the component without a logged-in user raises `djhtmx.exceptions.LoginRequired` before any handler or render runs, and each of the two paths that build components turns it into a trip to the login page instead of an HTTP 500.

The annotation decides what happens then:

- `user: Annotated[User, Field(exclude=True)]` -- the component refuses to exist, and the visitor lands on the login page.
- `user: Annotated[User | None, Field(exclude=True)]` -- the component is built with `user` set to `None`, so it renders for a visitor with no usable session instead of holding a user it must not act as.

`djhtmx.component.is_usable_user` is that question as a function, if your application needs to ask it too.

Components that read no user at all, or that still make sense to a viewer whose session died, opt out by keeping the field optional.

Use `djhtmx.component.requires_logged_user(SomeComponent)` to assert in your own tests which components require a login and which deliberately don't.
