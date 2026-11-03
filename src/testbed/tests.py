import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from djhtmx import json
from djhtmx.testing import Htmx

from .htmx import Board, Caption, Draft


class TestOverlappingRequests(TestCase):
    """A request of the page keeps what another request changed while it was running.

    The rename is held after it read the session, while the other request runs to its end; so the
    rename is the last to write, from what it read before the other request changed anything.

    """

    def setUp(self):
        self.htmx = Htmx(Client())
        self.htmx.navigate_to(reverse("testbed-draft"))
        self.draft = self.htmx.get_component_by_type(Draft)

    def test_a_request_keeps_the_field_that_another_request_changed(self):
        with self.renaming_in_another_request("Damper is stuck"):
            self.htmx.send(self.draft.attach_photo, photo="photo-cid")

        self.htmx.send(self.draft.render)
        draft = self.htmx.get_component_by_type(Draft)
        self.assertEqual((draft.name, draft.photos), ("Damper is stuck", ("photo-cid",)))

    def test_a_request_keeps_the_child_that_another_request_added(self):
        with self.renaming_in_another_request("Damper is stuck"):
            self.htmx.send(self.draft.add_caption)

        self.htmx.send(self.draft.render)
        [_caption] = self.htmx.get_components_by_type(Caption)
        self.htmx.send(self.draft.discard)
        self.assertEqual(list(self.htmx.get_components_by_type(Caption)), [])

    @contextmanager
    def renaming_in_another_request(self, name: str) -> Iterator[None]:
        """Rename the draft in a request that reads its session before the block, writes after."""
        [form] = self.htmx.select(f"#{self.draft.id}")
        [name_input] = self.htmx.select(f"#{self.draft.id} input[name=name]")
        headers = json.loads(form.attrib["hx-headers"])
        pauses = threading.Barrier(2, timeout=5)
        with (
            patch(f"{Draft.__module__}.renaming_pauses", pauses),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            renaming = pool.submit(
                Client().post, name_input.attrib["hx-post"], {"name": name}, headers=headers
            )
            pauses.wait()
            yield
            pauses.wait()
            self.assertEqual(renaming.result().status_code, 200)


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
