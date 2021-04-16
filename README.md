# djhtmx

Interactive UI Components for Django using [htmx](https://htmx.org)

## Install

Add `djhtmx` to your `INSTALLED_APPS` and add it to your `urls.py` as you whish, you can use any path.

```python
from django.urls import path, include

urlpatterns = [
    ...
    path('__htmx/', include('djhtmx.urls')),
    ...
]
```

In your base template you need to load the necesary scripts to make this work

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

This app will look for `live.py` files in your app, to register all your components, but if make the module where you have components gets loaded when Django boots up that alsoworks.

```python
from djhtmx.componet import Component

class Counter(Component):
   template_name = 'counter.html'

   def __init__(self, counter: int = 0, **kwargs):
       super().__init__(**kwargs)
       self.counter = counter

   def inc(self, amount: int):
       self.counter += amount
```

The `template.html` would be:

```html
{% load htmx %}
<div {% hx-tag %}>
  {{ counter }}
  <button {% on 'inc' %}>+</button>
  <button {% on 'inc' amount=2 %}>+2</button>
</div>
```

## What batteries are included

If you wanna use `hx-boost` go ahead and enable it with

```
...
<body {% hx_boost %}>
   ...
</body>
...
```

By default, djhtmx uses
[morphdom](https://github.com/patrick-steele-idem/morphdom) to make the
swapping.  You can use the argument `disable_morphdom` in the tags `hx-boost`
and `hx-tag` to selectively disable it; or use the setting
`DJHTMX_DISABLE_MORPHDOM` to False to disallow it completely.


## Python APIs

TODO

## Template tags

TODO

## Extended htmx attributes

These extensions are only available when using morphdom (this is the default).

- **hx-disabled**: Do not longer update a component after the first render.
- **hx-override**: The default behavor is that ff a swap happens while the user is focused on an input, that input does not get the `value` updated. Use this attribute to tag that input if you want the swap to also update the it's value, it will also lose the focus.
- **hx-added**: Add JavaScript here if you want it to be executed when the element is added.
- **hx-updated**: Add JavaScript here if you want it to be executed when the element is updated.
