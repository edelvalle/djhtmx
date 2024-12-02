# djhtmx

Interactive UI Components for Django using [htmx](https://htmx.org)

## Install

Add `djhtmx` to your `INSTALLED_APPS` and install the Middleware as the last one
of the list:

```python
INSTALLED_APPS = [
    ...
    'djhtmx',
    ...
]

MIDDLEWARE = [
    ...,
    'djhtmx.Middleware',
]

```

Expose the HTTP endpoint in your `urls.py` as you wish, you can use any path you want.

```python
from django.urls import path, include

urlpatterns = [
    # ...
    path('_htmx/', include('djhtmx.urls')),
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

This app will look for `htmx.py` files in your app and registers all components found there, but if you load any module where you have components manually when Django boots up, that also works.

```python
from djhtmx.component import PydanticComponent


class Counter(PydanticComponent):
    _template_name = 'Counter.html'
    counter: int = 0

    def inc(self, amount: int = 1):
        self.counter += amount
```

The `inc` event handler is ready to be called from the front-end to respond to an event.

The `counter.html` would be:

```html
{% load htmx %}
<div {% hx-tag %}>
  {{ counter }}
  <button {% on 'inc' %}>+</button>
  <button {% on 'inc' amount=2 %}>+2</button>
</div>
```

When the event is dispatched to the back-end the component state is reconstructed, the event handler called and then the full component is rendered back to the front-end.

Now use the component in any of your html templates, by passing attributes that are part of the component state:

```html
{% load htmx %} Counter: <br />
{% htmx 'Counter' %} Counter with init value 3:<br />
{% htmx 'Counter' counter=3 %}
```

##  Doing more complicated stuff

### Authentication

All components have a `self.user` representing the current logged in user or `None` in case the user is anonymous. If you wanna make sure your user is properly validated and enforced. You need to create a base component and annotate the right user:

```python
from typing import Annotated
from pydantic import Field
from djhtmx.component import PydanticComponent

class BaseComponent(PydanticComponent, public=False):
    user: Annotated[User, Field(exclude=True)]


class Counter(BaseComponent):
    _template_name = 'Counter.html'
    counter: int = 0

    def inc(self, amount: int = 1):
        self.counter += amount
```

### Non-public components

These are components that can't be instantiated using `{% htmx 'ComponentName' %}` because they are used to create some abstraction and reuse code.

Pass `public=False` in their declaration

```python
class BaseComponent(PydanticComponent, public=False):
    ...
```

## Component nesting

Components can contain components inside to decompose the behavior in more granular and specialized parts, for this you don't have to do anything but to a component inside the template of other component....

```python
class Items(PydanticComponent):
    _template_name = 'Items.html'

    def items(self):
        return Item.objects.all()

class ItemEntry(PydanticComponent):
    ...
    item: Item
    is_open: bool = False
    ...
```

```html: Items.html
{% load htmx %}

<ul {% hx-tag %}>
  {% for item in items %}
    {% htmx 'ItemEntry' item=item %}
  {% endfor %}
</ul>
```

In this case every time there is a render of the parent component all children components will also be re-rendered.

How can you preserve the state in the child components if there were some of them that were already had `is_open = True`? The state that is not passed directly during instantiation to the component is retrieved from the session, but the component needs to have consistent id. To do this you have to pass an `id` to the component.


```html: Items.html
{% load htmx %}

<ul {% hx-tag %}>
  {% for item in items %}
    {% htmx 'ItemEntry' id='item-'|add:item.id item=item %}
  {% endfor %}
</ul>
```

## Lazy lading

If you want some component to load lazily, you pass `lazy=True` where it is being instantiated.


```html: Items.html
{% load htmx %}

<ul {% hx-tag %}>
  {% for item in items %}
    {% htmx 'ItemEntry' id='item-'|add:item.id item=item lazy=True %}
  {% endfor %}
</ul>
```

This makes the component to be initialized, but instead of rendering the template in `_template_name` the template defined in `_template_name_lazy` will be rendered (you can override this). When the component arrives to the front-end it will trigger an event to render it self.


## Implicit parameters

When sending an event to the back-end sometimes you can pass the parameters explicitly to the event handler, and sometimes these are inputs the user is typing stuff on. The value of those inputs are passed implicitly if they nave a `name="..."` attribute.

```python
class Component(PydanticComponent):
    ...

    def create(self, name: str, is_active: bool = False):
        Item.objects.create(name=name, is_active=is_active)

```

```html
{% load htmx %}

<form {% hx-tag %} {% on 'submit' 'create' %}>
  <input type "text" name="name">
  <input type="checkbox" name="is_active">
  <button type="submit">Create!</button>
</form>
```

The parameters of any event handler are always converted by pydantic to the annotated types. It's suggested to properly annotate the event handler parameter with the more restrictive types you can.

### Data structures in implicit parameters

Suppose that you have a multiple choice list and you want to select multiple options, you can do this by suffixing the name with `[]` as in `choices[]`:

```python
class DeleteSelection(PydanticComponent):

    @property
    def items(self):
        return self.filter(owner=self.user)

    def delete(self, selected: list[UUID] | None = None):
        if selected:
            self.items.filter(id__in=selected).delete()
```

```html
{% load htmx %}

<form {% hx-tag %} {% on 'submit' 'delete' %}>
  <h1>Select items to be deleted</h1>
  {% for item in items %}
    <p>
      <input
        type="checkbox"
        name="selected[]"
        value="{{ item.id }}"
        id="checkbox-{{ item.id }}"
       />
       <label for="checkbox-{{ item.id }}">{{ item.name}}</label>

    </p>
  {% endfor %}
  <p><button type="submit">Delete selected</button></p>
</form>

```


## Commands

Each event handler in a component can yield commands for the library to execute. These are useful for skipping the default component render, redirecting the user, remove the component from the front-end, and updating other components.

### Redirects

Wanna redirect the user to some object url:
- If you have the url directly you can `yield Redirect(url)`.

- If you want Django to resolve the url automatically use: `yield Redirect.to(obj, *args, **kwargs)` as you would use `django.shortcuts.resolve_url`.

```python
from djhtmx.component import PydanticComponent, Redirect


class Component(PydanticComponent):
    ...

    def create(self, name: str):
        item = Item.objects.create(name=name)
        yield Redirect.to(item)
```

If you want to open the url in a new url use the `yield Open...` command with similar syntax to `Redirect`.

### Remove the current component from the interface

Sometimes you want to remove the component when it responds to an event, for that you need to `yield Destroy(component_id: str)`. You can also use this to remove any other component if you know their id.

```python
from djhtmx.component import PydanticComponent, Destroy


class Notification(PydanticComponent):
    ...

    def close(self):
        yield Destroy(self.id)
```


### Skip renders

Sometimes when reacting to a front-end event is handy to skip the default render of the current component, to achieve this do:

```python
from djhtmx.component import PydanticComponent, Redirect


class Component(PydanticComponent):
    ...

    def do_something(self):
        ...
        yield SkipRender(self)
```

### Partial Rendering

Sometimes you don't want to do a full component render, but a partial one. Specially if the user if typing somewhere to filter items and you don't wanna interfere with the user typing or focus. Here is the technique to do that:

```python
from djhtmx.component import PydanticComponent, Render

class SmartFilter(PydanticComponent):
    _template_name = 'SmartFilter.html'
    query: str = ""

    @property
    def items(self):
        items = Item.objects.all()
        if self.query:
            items = items.filter(name__icontains=self.query)
        return items

    def filter(self, query: str):
        self.query = query.trim()
        yield Render(self, template='SmartFilter_list.html')
```

```html: SmartFilter.html
{% load htmx %}

<div {% hx-tag %}>
  <input type="text" name="query" value="{{ query }}">
  {% include "SmartFilter_list.html" %}
  </div>
```

```html: SmartFilter_list.html
<ul id="{{ id }}-list" {% oob %}>
  {% for item in items %}
    <li><a href="{{ item.get_absolute_url }}">{{ item }}</a></li>
  {% empty %}
    <li>Nothing found!</li>
  {% endfor %}
</ul>
```

- Split the component in multiple templates, the main one and the partial ones.
- For readability prefix the name of the partials with the name of the parent.
- The partials need a single root HTML Element with an id and the `{% oob %}` tag next to it.
- When you wanna do the partial render you have to `yield Render(self, template=...)` with the name of the partial template, this will automatically skip the default full render and render the component with that partial template.

## Query Parameters & State

Coming back to the previous example let's say that we want to persist the state of the `query` in the URL, so in case the user refreshes the page or shares the link the state of the component is partially restored. For do the following:

```python
from typing import Annotated
from djhtmx.component import PydanticComponent
from djhtmx.query import Query


class SmartFilter(PydanticComponent):
    ...
    query: Annotated[str, Query("query")] = ""
    ...
```

Annotating with Query causes that if the state of the query is not explicitly passed to the component during instantiation it is taken from the query string of the current URL.

There can be multiple components subscribed to the same query parameter or to individual ones.

If you want now you can split this component in two, each with their own template:


```python
from typing import Annotated
from djhtmx.component import PydanticComponent, SkipRender
from djhtmx.query import Query

class SmartFilter(PydanticComponent):
    _template_name = 'SmartFilter.html'
    query: Annotated[str, Query("query")] = ""

    def filter(self, query: str):
        self.query = query.trim()
        yield SkipRender(self)

class SmartList(PydanticComponent):
    _template_name = 'SmartList.html'
    query: Annotated[str, Query("query")] = ""

    @property
    def items(self):
        items = Item.objects.all()
        if self.query:
            items = items.filter(name__icontains=self.query)
        return items
```

Instantiate next to each other:


```html
<div>
    ...
    {% htmx "SmartFilter" %}
    {% htmx "SmartList" %}
    ...
</div>
```

When the filter mutates the `query`, the URL is updated and the `SmartList` is awaken because the both point to the same query parameter, and will be re-rendered.

## Signals

Sometimes you modify a model and you want not just the current component to react to this, but also trigger re-renders of other components that are not directly related to the current one. For this signals are very convenient. These are strings that represent topics you can subscribe a component to and make sure it is rendered in case any of the topics it subscribed to is triggered.

Signal formats:
 - `app_label.modelname`: Some mutation happened to a model instance of this kind
 - `app_label.modelname.instance_pk`: Some mutation happened to this precise instance of model
 - `app_label.modelname.instance_pk.created`: This instance was created
 - `app_label.modelname.instance_pk.updated`: This instance was updated
 - `app_label.modelname.instance_pk.deleted`: This instance was deleted

When an instance is modified the mode specific and not so specific signals are triggered.
Together with them some other signals to related models are triggered.

Example: if we have a Todo list app with the models:


```python
class TodoList(Model):
    ...

class Item(Model):
    todo_list = ForeignKey(TodoList, related_name='items')
```

And from the list with id `932` you take a item with id `123` and update it all this signals will be triggered:

- `todoapp.item`
- `todoapp.item.123`
- `todoapp.item.123.updated`
- `todoapp.todolist.932.items`
- `todoapp.todolist.932.items.updated`


### How to subscribe to signals

Let's say you wanna count how many items there are in certain Todo list, but your component does not receive an update when the list is updated because it is out of it. You can do this.

```python
from djhtmx.component import PydanticComponent

class ItemCounter(PydanticComponent):
    todo_list: TodoList

    def subscriptions(self):
        return {
            f'todoapp.todolist.{self.todo_list.id}.items.deleted',
            f'todoapp.todolist.{self.todo_list.id}.items.created',
        }

    def count(self):
        return self.todo_list.items.count()
```

This will make this component re-render every time an item is added or removed from the list `todo_list`.

## Dispatching Events between components

Sometimes is handy to notify components in the same session that something changed and they need to perform the corresponding update and `Query()` nor Signals are very convenient for this. In this case you can `Emit` events and listen to them.

Find here an implementation of `SmartFilter` and `SmartItem` using this mechanism:

```python
from dataclasses import dataclass
from djhtmx.component import PydanticComponent, SkipRender, Emit


@dataclass(slots=True)
class QueryChanged:
    query: str


class SmartFilter(PydanticComponent):
    _template_name = 'SmartFilter.html'
    query: str = ""

    def filter(self, query: str):
        self.query = query.trim()
        yield Emit(QueryChanged(query))
        yield SkipRender(self)

class SmartList(PydanticComponent):
    _template_name = 'SmartList.html'
    query: str = ""

    def _handle_event(self, event: QueryChanged):
        self.query = event.query

    @property
    def items(self):
        items = Item.objects.all()
        if self.query:
            items = items.filter(name__icontains=self.query)
        return items
```

The library will look in all components if they define `_handle_event(event: ...)` and based on the annotation of `event` subscribe them to those events. This annotation can be a single type or a `Union` with multiple even types.

## Inserting a component somewhere

Let's say that we are making the TODO list app and we want that when a new item is added to the list there is not a full re-render of the whole list, just that the Component handling a single Item is added to the list.

```python
from djhtmx.component import PydanticComponent, SkipRender, BuildAndRender

class TodoListComponent(PydanticComponent):
    _template_name = 'TodoListComponent.html'
    todo_list: TodoList

    def create(self, name: str):
        item = self.todo_list.items.create(name=name)
        yield BuildAndRender.prepend(
            f'{self.id} .list',
            ItemComponent,
            id=f'item-{item.id}',
            item=item,
        )
        yield SkipRender(self)

class ItemComponent(PydanticComponent):
    ...
    item: Item
    ...
```

```html: TodoListComponent.html
{% load htmx %}
<div {% hx-tag %}>
  <form {% on "submit" "create"  %}>
      <input type="text" name="name">
  </form>
  <ul class="list">
    {% for item in items  %}
      {% htmx "ItemComponent" id='item-'|add:item.id item=item  %}
    {% endfor %}
  </ul>
</div>
```

Use the `BuildAndRender.<helper>(target: str, ...)` to send a component to be inserted somewhere or updated.


## Focusing an item after render

Let's say we want to put the focus in an input that inside the new ItemComponent rendered, for this use `yield Focus(target)`

```python
from djhtmx.component import PydanticComponent, SkipRender, BuildAndRender, Focus

class TodoListComponent(PydanticComponent):
    _template_name = 'TodoListComponent.html'
    todo_list: TodoList

    def create(self, name: str):
        item = self.todo_list.items.create(name=name)
        item_id = f'item-{item.id}'
        yield BuildAndRender.prepend(
            f'{self.id} .list',
            ItemComponent,
            id=item_id,
            item=item,
        )
        yield Focus(f'#{item_id} input')
        yield SkipRender(self)
```

## Sending Events to the DOM

Suppose you have a rich JavaScript library (graphs, maps, or anything...) in the front-end and you want to communicate something to it because it is subscribed to some dome event. For that you can use `yield DispatchDOMEvent(target, event, detail, ....)`


```python
from djhtmx.component import PydanticComponent, DispatchDOMEvent

class TodoListComponent(PydanticComponent):
    _template_name = 'TodoListComponent.html'
    todo_list: TodoList

    def create(self, name: str):
        item = self.todo_list.items.create(name=name)
        yield DispatchDOMEvent(
            '#leaflet-map',
            'new-item',
            {'id': item.id, 'name': item.name, 'geojson': item.geojson}
        )
```

This will trigger that event in the front-end when the request arrives allowing rich JavaScript components to react accordingly without full re-render.
