# djhtmx

Interactive UI Components for Django using [htmx](https://htmx.org)

## Install

Add `djhtmx` to your `INSTALLED_APPS` and add it to your `urls.py` as you whish, you can use any path.

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

Now use your component in one of your templates:

```html
{% load htmx %}

{% htmx 'Counter' amount=10 %}


## Getting started

This app will look for `live.py` files in your app and registers all components found there, but if you load any module where you have components manually when Django boots up, that also works.

```python
from djhtmx.component import Component


class Counter(Component):
    template_name = 'counter.html'

    def __init__(self, counter: int = 0, **kwargs):
        super().__init__(**kwargs)
        self.counter = counter

    def inc(self, amount: int):
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

## Exntended htmx attributes

- **hx-after-swap**: Add JavaScript here if you want it to be executed when the element is updated.
