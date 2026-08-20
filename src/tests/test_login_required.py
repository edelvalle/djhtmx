"""A non-optional `user` annotation is a login requirement, and djhtmx enforces it.

Annotating `user: User` (instead of leaving the optional annotation `HtmxComponent` provides) says
the component is meaningless without a logged-in user.  The annotation alone cannot enforce it --
Django model fields are validated with a `PlainValidator` that lets `None` through -- so components
used to run their handlers with `self.user` set to `None` and die deep in whatever they wrote,
losing the user's work with no feedback on screen.

Two answers replace that 500, one per way a component gets built, and neither implies the other:

- a request to the `/_htmx/` endpoints (the session died while the page stayed open, the user logged
  out in another tab, the POST arrived without cookies) is answered with the `HX-Redirect` htmx acts
  on, from `CommandProcessor.process`;
- a full page render is answered with a redirect to the login page, from the djhtmx middleware's
  `process_exception` hook -- which Django only consults because the hook is attached to the
  middleware closure, an integration nothing else in the suite covers.

"""

from typing import Annotated

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from fision.todo.htmx import LoggedUserCounter, TodoCounter  # type: ignore[import-untyped]
from pydantic import Field

from djhtmx.commands import Redirect, SkipRender
from djhtmx.component import HtmxComponent, requires_logged_user
from djhtmx.exceptions import LoginRequired
from djhtmx.introspection import ModelConfig
from djhtmx.middleware import middleware, process_exception
from djhtmx.repo import Repository, Session
from djhtmx.utils import get_params


class GuardedCounter(HtmxComponent):
    """The idiom under test: a user model annotation with no other declaration."""

    _template_name = "GuardedCounter.html"

    user: Annotated[User, Field(exclude=True)]
    counter: int = 0

    def inc(self):
        self.counter += 1
        # Skipping the render keeps the dispatch tests off the template: what they check is whether
        # the handler ran at all.
        yield SkipRender(self)


class InheritedGuardCounter(GuardedCounter):
    """A component that gets the requirement from its base, the way applications do."""


class LazyGuardedCounter(HtmxComponent):
    """A lazy user: the guard has to judge it without fetching the row."""

    _template_name = "LazyGuardedCounter.html"

    user: Annotated[User, ModelConfig(lazy=True), Field(exclude=True)]
    counter: int = 0

    def inc(self):
        self.counter += 1
        yield SkipRender(self)


class OptionalLazyUserCounter(HtmxComponent):
    """The optional case, deferred: an unusable user must collapse to `None` here too."""

    _template_name = "OptionalLazyUserCounter.html"

    user: Annotated[User | None, ModelConfig(lazy=True), Field(exclude=True)]
    counter: int = 0

    def inc(self):
        self.counter += 1
        yield SkipRender(self)


class OptionalUserCounter(HtmxComponent):
    """The opt-out: an explicitly optional user renders for an anonymous visitor."""

    _template_name = "OptionalUserCounter.html"

    user: Annotated[User | None, Field(exclude=True)]
    counter: int = 0

    def inc(self):
        self.counter += 1
        yield SkipRender(self)


class TestRequiresLoggedUser(SimpleTestCase):
    """Which annotations djhtmx reads as a login requirement."""

    def test_a_user_model_annotation_requires_a_login(self):
        self.assertTrue(requires_logged_user(GuardedCounter))

    def test_the_requirement_is_inherited(self):
        """Applications declare the annotation once on a base component and subclass it."""
        self.assertTrue(requires_logged_user(InheritedGuardCounter))

    def test_an_optional_user_requires_nothing(self):
        self.assertFalse(requires_logged_user(OptionalUserCounter))

    def test_a_component_that_declares_no_user_requires_nothing(self):
        """The annotation inherited from `HtmxComponent` must stay the permissive default."""
        self.assertFalse(requires_logged_user(TodoCounter))


class TestBuild(TestCase):
    """Building the component is what raises, before any handler or render runs."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="skipper")

    def test_a_logged_user_builds_the_component(self):
        component = self.repository(self.user).build("GuardedCounter", {"id": "guarded"})
        self.assertEqual(component.user, self.user)

    def test_an_anonymous_request_raises(self):
        with self.assertRaises(LoginRequired) as raised:
            self.repository(AnonymousUser()).build("GuardedCounter", {"id": "guarded"})
        self.assertIn(GuardedCounter.__name__, raised.exception.component_name)

    def test_an_inherited_requirement_raises_too(self):
        with self.assertRaises(LoginRequired):
            self.repository(AnonymousUser()).build("InheritedGuardCounter", {"id": "inherited"})

    def test_an_optional_user_builds_for_an_anonymous_request(self):
        component = self.repository(AnonymousUser()).build("OptionalUserCounter", {"id": "open"})
        self.assertIsNone(component.user)

    def test_an_unsaved_user_is_not_a_logged_user(self):
        """A user with no primary key cannot own anything the component would write."""
        with self.assertRaises(LoginRequired):
            self.repository(User(username="unsaved")).build("GuardedCounter", {"id": "guarded"})

    def repository(self, user) -> Repository:
        return Repository(
            user=user,
            session=Session(Repository.new_session_id()),
            params=get_params(None),
        )


class TestUserProtocol(TestCase):
    """The `user` protocol: a user who cannot act is no user at all.

    Three inputs mean "nobody is logged in": no user, a primary key that matches no row, and a user
    whose account is not active.  A component that requires a user refuses all three; one that
    admits `None` sees `None` for all three, rather than holding a user it must not act as.

    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="skipper")
        cls.inactive = User.objects.create_user(username="retired", is_active=False)

    def test_a_required_user_refuses_an_inactive_account(self):
        """`is_active` is the same question Django's own backend asks before authenticating.

        A session cookie never gets this far -- `ModelBackend.get_user` already returns `None` for an
        inactive account -- but a request authenticated by other means (a token middleware that loads
        the row itself) hands over a real, unusable user.
        """
        for component in (GuardedCounter, LazyGuardedCounter):
            with self.subTest(component=component.__name__), self.assertRaises(LoginRequired):
                component(hx_name=component.__name__, user=self.inactive)

    def test_an_optional_user_becomes_none_when_it_cannot_act(self):
        """Every unusable value collapses to `None`, so the component renders as anonymous."""
        missing_pk = self.user.pk + 10_000
        for value, label in ((self.inactive, "inactive"), (None, "none"), (missing_pk, "missing")):
            for component in (OptionalUserCounter, OptionalLazyUserCounter):
                with self.subTest(component=component.__name__, value=label):
                    built = component(hx_name=component.__name__, user=value)
                    self.assertIsNone(built.user)

    def test_a_required_lazy_user_refuses_a_primary_key_with_no_row(self):
        """The account was deleted while a state that names it is still in play."""
        with self.assertRaises(LoginRequired):
            LazyGuardedCounter(hx_name="LazyGuardedCounter", user=self.user.pk + 10_000)

    def test_a_live_user_is_left_alone(self):
        """The control: the protocol must not reject the case it exists to allow."""
        for component in (GuardedCounter, LazyGuardedCounter):
            with self.subTest(component=component.__name__):
                built = component(hx_name=component.__name__, user=self.user)
                self.assertEqual(built.user.pk, self.user.pk)

    def test_the_check_reads_no_row_for_a_lazy_user_the_request_supplied(self):
        """The guard must not defeat the laziness it guards.

        A request always hands over the user *instance*, so the proxy already holds it and the
        protocol reads `is_active` off it for free.  Only a component built from a bare primary key
        pays a query, and `user` is excluded from component state, so no request rebuilds one.
        """
        with self.assertNumQueries(0):
            component = LazyGuardedCounter(hx_name="LazyGuardedCounter", user=self.user)
            self.assertEqual(component.user.pk, self.user.pk)


class TestDispatch(TestCase):
    """What an event dispatched on a dead session answers, and that the handler stays unrun."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="skipper")

    def setUp(self):
        """Mount the component as the logged-in user; each test then dispatches its own way.

        The state has to be in the session for the dispatch to reach the component at all --
        without it djhtmx answers with a `Destroy` for an unknown component and never builds
        anything.
        """
        super().setUp()
        session = Session(Repository.new_session_id())
        repository = Repository(user=self.user, session=session, params=get_params(None))
        component = repository.build("GuardedCounter", {"id": "guarded"})
        session.store(component)
        session.flush()
        self.session_id = session.id

    def test_an_anonymous_dispatch_redirects_to_the_login_page(self):
        commands = list(self.dispatch(AnonymousUser()))

        self.assertEqual(commands, [Redirect(settings.LOGIN_URL)])
        self.assertEqual(self.stored_counter(), 0, "the handler must not have run")

    def test_the_same_dispatch_with_a_live_session_still_runs(self):
        """The control: a guard that redirected every dispatch would pass the test above."""
        commands = list(self.dispatch(self.user))

        self.assertNotIn(Redirect(settings.LOGIN_URL), commands)
        self.assertEqual(self.stored_counter(), 1)

    def dispatch(self, user):
        repository = Repository(
            user=user,
            session=Session(self.session_id),
            params=get_params(None),
        )
        return repository.dispatch_event("guarded", "inc", {})

    def stored_counter(self) -> int:
        state = Session(self.session_id).get_state("guarded")
        assert state is not None
        return state["counter"]


class TestPageRender(TestCase):
    """A page mounting the component, rendered by the real view and middleware stack."""

    URL = "/logged-user-counter"

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="skipper", password="skipper")

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self):
        response = Client().get(self.URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"{settings.LOGIN_URL}?next={self.URL}")

    def test_a_logged_visitor_gets_the_page(self):
        client = Client()
        client.force_login(self.user)

        response = client.get(self.URL)

        self.assertEqual(response.status_code, 200)
        self.assertIn(LoggedUserCounter.__name__, response.content.decode())


class TestMiddlewareHook(SimpleTestCase):
    """The `process_exception` hook itself: Django only calls it if it is on the closure."""

    def test_the_hook_is_attached_to_the_middleware(self):
        self.assertIs(middleware(lambda request: None).process_exception, process_exception)  # type: ignore[arg-type, attr-defined]

    def test_other_exceptions_are_left_alone(self):
        """Returning `None` is what keeps unrelated failures reported as the 500s they are."""
        request = RequestFactory().get("/anything")

        self.assertIsNone(process_exception(request, ValueError("boom")))
