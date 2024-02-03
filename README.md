# djhtmx

Interactive UI Components for Django using [htmx](https://htmx.org)

## Install

Add `djhtmx` to your `INSTALLED_APPS` and add it to your `urls.py` as you wish, you can use any path.

```python
from django.urls import path, include

urlpatterns = [
    # ...
    path('__htmx/', include('djhtmx.urls')),
    # ...
]
```

In your base template you need to load the necessary scripts to make this work

```html
{% load htmx %}
<!doctype html>
<html>
    <head>
        {% htmx-headers %}
    </head>
</html>
```

## Getting started

This app will look for `live.py` files in your app and registers all components found there, but if you load any module where you have components manually when Django boots up, that also works.

```python
from djhtmx.component import PydanticComponent


class Counter(PydanticComponent):
    template_name = 'counter.html'
    counter: int = 0

    def inc(self, amount: int = 1):
        self.counter += amount
```

The `counter.html` would be:

```html
{% load htmx %}
<div {% hx-tag %}>
  {{ counter }}
  <button {% on 'inc' %}>+</button>
  <button {% on 'inc' amount=2 %}>+2</button>
</div>
```

Now use the component in any of your html templates:

```html
{% load htmx %} Counter: <br />
{% htmx 'Counter' %} Counter with init value 3:<br />
{% htmx 'Counter' counter=3 %}
```

## What batteries are included

This project mixes htmx with morphdom for a more smooth rendering a find control of the focus when this one is on an input.

If you wanna use `hx-boost` go ahead and enable it with:

```
...
<body hx-boost="true">
   ...
</body>
...
```

## Python APIs

TODO

## Template tags

TODO

## Extended htmx attributes

-   **hx-after-swap**: Add JavaScript here if you want it to be executed when the element is updated.
