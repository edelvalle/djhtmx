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

