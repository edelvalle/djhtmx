"""The kind recorded for a handler describes the handler, not the wrapper around it.

`validate_call`:func: wraps every event handler that declares parameters, and the wrapper is an
ordinary function: asked whether it is a generator it answers about itself.  The registry reads the
shapes while the component registers, which for a handler *declared* on that component runs before
the wrapping loop -- but a handler reached through a base class was already wrapped when that base
registered, and no later pass can see through it.

These are the cases where the two differ: the same four handlers declared on a component, inherited
from a public component, and inherited from a non-public base -- the mixin pattern applications use.

"""

from django.test import SimpleTestCase

from djhtmx.commands import SkipRender
from djhtmx.component import REGISTRY, HandlerKind, HtmxComponent


class HandlerKinds(HtmxComponent):
    """The four shapes, declared here: with and without parameters, plain and generator."""

    _template_name: str = "HandlerKinds.html"

    counter: int = 0

    def plain(self):
        self.counter += 1

    def plain_with_args(self, amount: int = 1):
        self.counter += amount

    def gen(self):
        self.counter += 1
        yield SkipRender(self)

    def gen_with_args(self, amount: int = 1):
        self.counter += amount
        yield SkipRender(self)


class InheritedHandlerKinds(HandlerKinds):
    """The same four handlers, reached through a public base that has already wrapped them."""


class BaseHandlerKinds(HtmxComponent, public=False):
    """A non-public base: it never registers, but its own wrapping pass still runs."""

    counter: int = 0

    def gen(self):
        self.counter += 1
        yield SkipRender(self)

    def gen_with_args(self, amount: int = 1):
        self.counter += amount
        yield SkipRender(self)


class HandlerKindsFromBase(BaseHandlerKinds):
    """The mixin pattern: handlers live on the base, registration happens here."""

    _template_name: str = "HandlerKindsFromBase.html"


class HandlerKindTests(SimpleTestCase):
    def assertKinds(self, component_name: str, **expected: HandlerKind):
        mapping = REGISTRY[component_name].handler_kind_mapping
        for handler_name, kind in expected.items():
            with self.subTest(component=component_name, handler=handler_name):
                self.assertEqual(mapping[handler_name], kind)

    def test_own_handlers(self):
        self.assertKinds(
            "HandlerKinds",
            plain="function",
            plain_with_args="function",
            gen="generator",
            gen_with_args="generator",
        )

    def test_handlers_inherited_from_a_public_component(self):
        self.assertKinds(
            "InheritedHandlerKinds",
            plain="function",
            plain_with_args="function",
            gen="generator",
            gen_with_args="generator",
        )

    def test_handlers_inherited_from_a_non_public_base(self):
        self.assertKinds(
            "HandlerKindsFromBase",
            gen="generator",
            gen_with_args="generator",
        )
