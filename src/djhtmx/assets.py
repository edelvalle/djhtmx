import dataclasses
import typing as t
from abc import abstractmethod
from dataclasses import dataclass
from urllib.parse import quote, urljoin

from django.apps import apps
from django.utils.encoding import iri_to_uri
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString, mark_safe


@dataclass(slots=True, unsafe_hash=True)
class Asset:
    @abstractmethod
    def render(self) -> SafeString: ...

    @property
    @abstractmethod
    def ordering(self) -> str:
        """A static or dynamic relative ordering of the assets.

        This basically influences the order of all assets in the <head>.

        """
        ...

    @classmethod
    def get_settings(cls, name):
        try:
            from django.conf import settings
        except ImportError:
            return ""
        else:
            return iri_to_uri(getattr(settings, name, ""))

    @classmethod
    def resolve_static(cls, path):
        if apps.is_installed("django.contrib.staticfiles"):
            from django.contrib.staticfiles.storage import staticfiles_storage

            return staticfiles_storage.url(path)
        else:
            return urljoin(cls.get_settings("STATIC_URL"), quote(path))

    def _render(self, tag_name, close_tag: bool = False) -> SafeString:
        """Render the tag as a safe string."""
        attributes = dataclasses.asdict(self)
        attributes["as"] = attributes.pop("as_", None)
        attrs = format_html_join(
            " ",
            "{}='{}'",
            (
                (mark_safe(key), value)
                for key, value in attributes.items()
                if value
            ),
        )
        if not close_tag:
            return format_html("<{} {}>", mark_safe(tag_name), attrs)
        else:
            return format_html(
                "<{} {}></{}>",
                mark_safe(tag_name),
                attrs,
                mark_safe(tag_name),
            )


@dataclass(slots=True, unsafe_hash=True)
class Link(Asset):
    """A <link> tag.

    https://developer.mozilla.org/en-US/docs/Web/HTML/Element/link

    """

    href: str
    # https://developer.mozilla.org/en-US/docs/Web/HTML/Attributes/rel
    rel: str | None = None
    integrity: str | None = None
    as_: str | None = None
    crossorigin: str | None = None

    @property
    def ordering(self) -> str:
        return "head/000-link"

    @classmethod
    def from_static(cls, href: str, **kwargs):
        """Create the link by lookup the statics."""
        return cls(cls.resolve_static(href), **kwargs)

    def render(self) -> SafeString:
        """Render the tag as a safe string.

        Example::

          >>> from djhtmx.assets import Link
          >>> lnk = Link("https://developer.mozilla.org/en-US/docs/Web/HTML/Element/link", rel="preload")
          >>> lnk.render()
          "<link href='https://developer.mozilla.org/en-US/docs/Web/HTML/Element/link' rel='preload'>"

        """
        return self._render("link")


@dataclass(slots=True, unsafe_hash=True)
class Script(Asset):
    """A <script> tag with a possible preload.

    https://developer.mozilla.org/en-US/docs/Web/HTML/Element/script

    """

    src: str
    integrity: str | None = None
    type: t.Literal["module", "importrule"] | None = None  # there are more!
    defer: bool = False
    crossorigin: t.Literal["anonymous", "use-credentials", ""] | None = None

    @classmethod
    def from_static(cls, src: str, **kwargs):
        """Create the script by lookup the statics."""
        return cls(cls.resolve_static(src), **kwargs)

    def render(self) -> SafeString:
        """Render the tag as a safe string.

        Example::

          >>> from djhtmx.assets import Script
          >>> script = Script("https://cdn.tailwindcss.com")
          >>> script.render()
          "<script src='https://cdn.tailwindcss.com'></script>"

        """
        return self._render("script", close_tag=True)

    @property
    def ordering(self) -> str:
        return "head/900-script"
