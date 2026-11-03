from djhtmx.commands import BuildAndRender, Render, SkipRender
from djhtmx.component import HtmxComponent


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
