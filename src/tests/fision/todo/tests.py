from django.test import Client, TestCase

from djhtmx.testing import Htmx

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
        self.htmx.type("input.new-todo", "3rd task")
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
        self.htmx.type("li.editing input.edit", "New name", clear=True)
        self.htmx.trigger("li.editing input.edit")

        # ensure new name is set
        self.assertFalse(self.htmx.select("li.editing"))
        [a, c] = self.htmx.select('[hx-name="TodoItem"] label')
        self.assertEqual(a.text_content(), "First task")
        self.assertEqual(c.text_content(), "New name")
