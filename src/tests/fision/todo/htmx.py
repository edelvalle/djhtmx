import typing as t
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field

from djhtmx.component import PydanticComponent, Query

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


class BaseToggleFilter(PydanticComponent):
    showing: t.Annotated[Showing, Query("showing"), Field(default=Showing.ALL)]


class BaseQueryFilter(PydanticComponent):
    query: str = ""

    def _handle_event(self, event: FilterChanged):
        self.query = event.query


class TodoList(BaseToggleFilter, BaseQueryFilter):
    _template_name = "todo/list.html"

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
    _template_name = "todo/list_header.html"

    def _handle_event(self, event: ItemsCleared | int):
        pass

    def add(self, new_item: str):
        item = Item.objects.create(text=new_item)
        self.controller.append(
            "#todo-list",
            TodoItem,
            id=f"item-{item.id}",
            item=item,
        )


class TodoItem(PydanticComponent):
    _template_name = "todo/item.html"

    item: Item
    editing: bool = False

    def delete(self):
        self.item.delete()
        self.controller.destroy()

    def completed(self, completed: bool = False):
        self.item.completed = completed
        self.item.save()

    def toggle_editing(self):
        if not self.item.completed:
            self.editing = not self.editing
        if self.editing:
            self.controller.focus(f"#{self.id} input[name=text]")

    def save(self, text):
        self.item.text = text
        self.item.save()
        self.editing = False


class TodoCounter(PydanticComponent):
    _template_name = "todo/counter.html"

    query: t.Annotated[str, Query("q")] = ""

    @property
    def subscriptions(self) -> set[str]:
        return {"todo.item"}

    @property
    def items(self):
        return Item.objects.active()


class TodoFilter(PydanticComponent):
    _template_name = "todo/filter.html"
    query: t.Annotated[str, Query("q")] = ""

    def set_query(self, query: str = ""):
        self.query = query = query.strip()
        self.controller.emit(FilterChanged(query))
