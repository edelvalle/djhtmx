import random
import typing as t
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field

from djhtmx.component import BuildAndRender, Destroy, Emit, Focus, PydanticComponent, Query

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


class BaseToggleFilter(PydanticComponent, public=False):
    showing: t.Annotated[Showing, Query("showing"), Field(default=Showing.ALL)]


class BaseQueryFilter(PydanticComponent, public=False):
    query: str = ""

    def _handle_event(self, event: FilterChanged):
        self.query = event.query


class TodoList(BaseToggleFilter, BaseQueryFilter):
    _template_name = "todo/TodoList.html"

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
    def all_items_are_completed(self):
        return self.items.count() == self.items.completed().count()

    def toggle_all(self, toggle_all: bool = False):
        self.items.update(completed=toggle_all)

    def show(self, showing: Showing):
        self.showing = showing

    def clear_completed(self):
        self.items.completed().delete()


class ListHeader(PydanticComponent):
    _template_name = "todo/ListHeader.html"

    def _handle_event(self, event: ItemsCleared | int):
        pass

    def add(self, new_item: str):
        item = Item.objects.create(text=new_item)
        yield BuildAndRender.append("#todo-list", TodoItem, id=f"item-{item.id}", item=item)


class TodoItem(PydanticComponent):
    _template_name = "todo/TodoItem.html"

    item: Item
    editing: bool = False

    def render(self):
        from time import sleep

        sleep(random.random() * 2 + 0.1)

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

    def save(self, text):
        self.item.text = text
        self.item.save()
        self.editing = False


class TodoCounter(PydanticComponent):
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


class TodoFilter(PydanticComponent):
    _template_name = "todo/TodoFilter.html"
    query: t.Annotated[str, Query("q")] = ""

    def set_query(self, query: str = ""):
        self.query = query.strip()
        yield Emit(FilterChanged(self.query))
