from enum import StrEnum

from djhtmx.component import PydanticComponent

from .models import Item


class Showing(StrEnum):
    ALL = "all"
    COMPLETED = "completed"
    ACTIVE = "active"


class TodoList(PydanticComponent):
    _template_name = "todo/list.html"

    showing: Showing = Showing.ALL

    @property
    def queryset(self):
        return Item.objects.all()

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

    def toggle_all(self, toggle_all: bool):
        self.items.update(completed=toggle_all)

    def show(self, showing: Showing):
        self.showing = showing
        self.controller.params["showing"] = showing

    def clear_completed(self):
        self.items.completed().delete()


class ListHeader(PydanticComponent):
    _template_name = "todo/list_header.html"

    def add(self, new_item: str):
        item = Item.objects.create(text=new_item)
        todo_item = self.controller.build(
            TodoItem, id=f"item-{item.id}", item=item
        )
        self.controller.append("#todo-list", todo_item)


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

    @property
    def subscriptions(self) -> set[str]:
        return {"todo.item"}

    @property
    def items(self):
        return Item.objects.active()
