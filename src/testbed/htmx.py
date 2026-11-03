import threading

from djhtmx.commands import BuildAndRender, Destroy, Render, SkipRender
from djhtmx.component import HtmxComponent

# A test swaps in a barrier of two parties to hold a rename between its read of the session and
# its flush; a barrier of one party never blocks.
renaming_pauses = threading.Barrier(1)


class Draft(HtmxComponent):
    """A form that two requests of its page change at the same time."""

    _template_name = "testbed/Draft.html"

    name: str = ""
    photos: tuple[str, ...] = ()

    def rename(self, name: str):
        renaming_pauses.wait()
        renaming_pauses.wait()
        self.name = name

    def attach_photo(self, photo: str):
        self.photos = (*self.photos, photo)

    def add_caption(self):
        yield BuildAndRender.append(f"#{self.id}", Caption, parent_id=self.id)

    def discard(self):
        yield Destroy(self.id)


class Caption(HtmxComponent):
    _template_name = "testbed/Caption.html"


class Board(HtmxComponent):
    """A component whose handlers answer with out-of-band swaps only."""

    _template_name = "testbed/Board.html"

    def show_new_list(self):
        yield Render(self, template="testbed/Board-list.html")

    def add_note_before_anchor(self):
        # A full render would drop the notes added before.
        yield SkipRender(self)
        yield BuildAndRender.before("#anchor", Note)


class Note(HtmxComponent):
    _template_name = "testbed/Note.html"
