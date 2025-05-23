import random
import typing as t
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field

from djhtmx.component import BuildAndRender, Destroy, Emit, Focus, HtmxComponent, Query, SkipRender

from .models import Item


@dataclass
class ItemsCleared:
    pass


class Showing(StrEnum):
    ALL = "all"
    COMPLETED = "completed"
    ACTIVE = "active"


@dataclass(slots=True)
class FilterChanged:
    query: str


class BaseToggleFilter(HtmxComponent, public=False):
    showing: t.Annotated[Showing, Query("showing"), Field(default=Showing.ALL)]


class BaseQueryFilter(HtmxComponent, public=False):
    query: str = ""

    def _handle_event(self, event: FilterChanged):
        self.query = event.query


@dataclass(slots=True)
class SetEditing:
    item: Item | None


class TodoList(BaseToggleFilter, BaseQueryFilter):
    _template_name = "todo/TodoList.html"
    editing: t.Annotated[Item | None, Query("editing")] = None

    def _handle_event(self, event: SetEditing | FilterChanged):
        if isinstance(event, SetEditing):
            self.editing = event.item
            yield SkipRender(self)
        else:
            super()._handle_event(event)

    @property
    def queryset(self):
        if not self.query:
            return Item.objects.all()
        else:
            return Item.objects.filter(text__icontains=self.query)

    @property
    def items(self):
        match self.showing:
            case Showing.ALL:
                qs = self.queryset
            case Showing.COMPLETED:
                qs = self.queryset.filter(completed=True)
            case Showing.ACTIVE:
                qs = self.queryset.filter(completed=False)
        return qs

    @property
    def editing_items(self):
        return [(item, item == self.editing) for item in self.items]

    @property
    def all_items_are_completed(self):
        return self.items.count() == self.items.completed().count()

    def toggle_all(self, toggle_all: bool = False):
        self.items.update(completed=toggle_all)

    def show(self, showing: Showing):
        self.showing = showing

    def clear_completed(self):
        self.items.completed().delete()


class ListHeader(HtmxComponent):
    _template_name = "todo/ListHeader.html"

    def _handle_event(self, event: ItemsCleared | int):
        pass

    def add(self, new_item: str):
        item = Item.objects.create(text=new_item)
        yield BuildAndRender.append("#todo-list", TodoItem, id=f"item-{item.id}", item=item)


class TodoItem(HtmxComponent):
    _template_name = "todo/TodoItem.html"

    item: Item
    editing: bool = False

    def delete(self):
        self.item.delete()
        yield Destroy(self.id)

    def completed(self, completed: bool = False):
        self.item.completed = completed
        self.item.save()

    def toggle_editing(self):
        if not self.item.completed:
            self.editing = not self.editing
        if self.editing:
            yield Focus(f"#{self.id} input[name=text]")
            yield Emit(SetEditing(item=self.item))
        else:
            yield Emit(SetEditing(item=None))

    def save(self, text):
        self.item.text = text
        self.item.save()
        if self.editing:
            yield from self.toggle_editing()


class TodoCounter(HtmxComponent):
    _template_name = "todo/TodoCounter.html"

    query: t.Annotated[str, Query("q")] = ""

    def render(self):
        from time import sleep

        sleep(random.random() * 3 + 0.5)

    @property
    def subscriptions(self) -> set[str]:
        return {"todo.item"}

    @property
    def items(self):
        return Item.objects.active()


class TodoFilter(HtmxComponent):
    _template_name = "todo/TodoFilter.html"
    query: t.Annotated[str, Query("q")] = ""

    def set_query(self, query: str = ""):
        self.query = query.strip()
        yield Emit(FilterChanged(self.query))
