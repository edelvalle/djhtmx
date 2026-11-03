from django.test import Client, TestCase
from django.urls import reverse

from djhtmx.testing import Htmx

from .htmx import Board


class TestOutOfBandSwaps(TestCase):
    """The DOM after a send is the one htmx 2 makes in the browser for the same response."""

    def setUp(self):
        self.htmx = Htmx(Client())
        self.htmx.navigate_to(reverse("testbed-board"))
        self.board = self.htmx.get_component_by_type(Board)

    def test_an_oob_element_nested_in_the_rendered_html_is_swapped(self):
        self.htmx.send(self.board.show_new_list)

        [list_] = self.htmx.select(f"#{self.board.id}-list")
        self.assertEqual(list_.text_content(), "new")
        self.assertEqual(len(self.htmx.select(".wrapper")), 1)

    def test_build_and_render_before_inserts_before_the_target(self):
        self.htmx.send(self.board.add_note_before_anchor)

        [note] = self.htmx.select('[hx-name="Note"]')
        self.assertEqual(note.getnext().attrib["id"], "anchor")
