from django.test import Client, TestCase

from djhtmx.commands import Emit, Focus, SkipRender
from djhtmx.testing import Htmx

from .htmx import TodoItem, TodoList
from .models import Item


class TestNormalRendering(TestCase):
    def setUp(self):
        Item.objects.create(text="First task")
        Item.objects.create(text="Second task")
        self.htmx = Htmx(Client())

    def test_stuff(self):
        self.htmx.navigate_to("/todo")

        [a, b] = self.htmx.select('[hx-name="TodoItem"] label')
        self.assertEqual(a.text_content(), "First task")
        self.assertEqual(b.text_content(), "Second task")

        [count] = self.htmx.select(".todo-count")
        self.assertEqual(count.text_content(), "2 items left")

        # Add new item
        self.htmx.type_into("input.new-todo", "3rd task")
        self.htmx.trigger("input.new-todo")

        [count] = self.htmx.select(".todo-count")
        self.assertEqual(count.text_content(), "3 items left")

        [a, b, c] = self.htmx.select('[hx-name="TodoItem"] label')
        self.assertEqual(a.text_content(), "First task")
        self.assertEqual(b.text_content(), "Second task")
        self.assertEqual(c.text_content(), "3rd task")

        # Mark first item as completed
        complete_task, *_ = self.htmx.select("input.toggle")
        self.htmx.trigger(complete_task)

        self.assertEqual(len(self.htmx.select('li.completed[hx-name="TodoItem"]')), 1)

        [count] = self.htmx.select(".todo-count")
        self.assertEqual(count.text_content(), "2 items left")

        # Show active items
        self.assertEqual(self.htmx.query_string, "")
        [active] = self.htmx.find_by_text("Active")
        self.htmx.trigger(active)
        self.assertEqual(self.htmx.query_string, "showing=active")

        [b, c] = self.htmx.select('[hx-name="TodoItem"] label')
        self.assertEqual(b.text_content(), "Second task")
        self.assertEqual(c.text_content(), "3rd task")

        # Show completed items
        [completed] = self.htmx.find_by_text("Completed")
        self.htmx.trigger(completed)
        self.assertEqual(self.htmx.query_string, "showing=completed")

        [a] = self.htmx.select('[hx-name="TodoItem"] label')
        self.assertEqual(a.text_content(), "First task")

        # Show all
        [show_all] = self.htmx.find_by_text("All")
        self.htmx.trigger(show_all)
        self.assertEqual(self.htmx.query_string, "")

        [a, b, c] = self.htmx.select('[hx-name="TodoItem"] label')
        self.assertEqual(a.text_content(), "First task")
        self.assertEqual(b.text_content(), "Second task")
        self.assertEqual(c.text_content(), "3rd task")

        # Delete second task
        [_, b_destroy, c] = self.htmx.select("button.destroy")
        self.htmx.trigger(b_destroy)
        [a, c] = self.htmx.select('[hx-name="TodoItem"] label')
        self.assertEqual(a.text_content(), "First task")
        self.assertEqual(c.text_content(), "3rd task")

        # Click to edit input
        self.assertFalse(self.htmx.select("li.editing"))
        [_, b_edit] = self.htmx.select('[hx-name="TodoItem"] label')
        self.htmx.trigger(b_edit)
        self.assertEqual(len(self.htmx.select("li.editing")), 1)

        # type new name and save!
        self.htmx.type_into("li.editing input.edit", "New name", clear=True)
        self.htmx.trigger("li.editing input.edit")

        # ensure new name is set
        self.assertFalse(self.htmx.select("li.editing"))
        [a, c] = self.htmx.select('[hx-name="TodoItem"] label')
        self.assertEqual(a.text_content(), "First task")
        self.assertEqual(c.text_content(), "New name")


class TestCapturing(TestCase):
    """What a test sees of the commands a dispatch produced."""

    def setUp(self):
        Item.objects.create(text="First task")
        self.htmx = Htmx(Client())
        self.htmx.navigate_to("/todo")
        self.todo_item = self.htmx.get_component_by_type(TodoItem)

    def test_captures_the_watched_classes_in_yield_order(self):
        # The classes are named in the reverse order of the yields, so a capture following its
        # arguments instead of the dispatch would answer the other way around.
        with self.htmx.capturing(SkipRender, Focus) as captured:
            self.htmx.send(self.todo_item.toggle_editing)
        [focus, skip_render] = captured
        self.assertIsInstance(focus, Focus)
        self.assertIsInstance(skip_render, SkipRender)

    def test_capturing_no_class_captures_every_command(self):
        with self.htmx.capturing() as captured:
            self.htmx.send(self.todo_item.toggle_editing)
        [focus, emit, skip_render] = captured
        self.assertIsInstance(focus, Focus)
        self.assertIsInstance(emit, Emit)
        self.assertIsInstance(skip_render, SkipRender)

    def test_commands_of_other_classes_are_a_yield_all_the_same(self):
        with self.htmx.capturing(Emit) as captured:
            self.htmx.send(self.todo_item.completed, completed=True)
        self.assertEqual(list(captured), [])
        self.assertTrue(captured.did_yield)

    def test_handlers_that_yielded_nothing_did_not_yield(self):
        todo_list = self.htmx.get_component_by_type(TodoList)
        with self.htmx.capturing() as captured:
            self.htmx.send(todo_list.toggle_all, toggle_all=True)
        self.assertEqual(list(captured), [])
        self.assertFalse(captured.did_yield)

    def test_assert_yields_hands_over_the_commands_of_its_class(self):
        with self.htmx.assertYields(Focus) as commands:
            self.htmx.send(self.todo_item.toggle_editing)
        [focus] = commands
        self.assertEqual(focus.selector, f"#{self.todo_item.id} input[name=text]")

    def test_assert_yields_names_what_was_yielded_instead(self):
        with self.assertRaises(AssertionError) as failure, self.htmx.assertYields(Emit):
            self.htmx.send(self.todo_item.completed, completed=True)
        self.assertIn("No Emit was yielded inside the block", str(failure.exception))
        self.assertIn("TodoItem.completed -> SkipRender", str(failure.exception))

    def test_assert_yields_none_names_what_was_yielded(self):
        with self.assertRaises(AssertionError) as failure, self.htmx.assertYields(None):
            self.htmx.send(self.todo_item.toggle_editing)
        self.assertIn("Expected nothing to be yielded inside the block", str(failure.exception))
        self.assertIn("TodoItem.toggle_editing -> Focus", str(failure.exception))
