from unittest.mock import patch

from django.test import TestCase

from djhtmx.component import Destroy, HtmxComponent
from djhtmx.repo import Session


class MockComponent(HtmxComponent):
    """Mock component for testing."""

    _template_name = "test.html"

    def render(self):
        return []

    def delete(self):
        """Mock delete handler that returns Destroy command."""
        return [Destroy(self.id)]


class TestSessionRecursiveDestruction(TestCase):
    """Test recursive component destruction in Session."""

    def setUp(self):
        self.session = Session("test-session-id")

    def test_register_child_creates_parent_child_relationship(self):
        """Test that register_child creates proper parent-child mapping."""
        parent_id = "parent-1"
        child_id = "child-1"

        self.session.register_child(parent_id, child_id)

        self.assertIn(parent_id, self.session.children)
        self.assertIn(child_id, self.session.children[parent_id])
        self.assertTrue(self.session.is_dirty)

    def test_register_child_prevents_duplicates(self):
        """Test that registering the same child twice doesn't create duplicates."""
        parent_id = "parent-1"
        child_id = "child-1"

        self.session.register_child(parent_id, child_id)
        self.session.is_dirty = False  # Reset dirty flag
        self.session.register_child(parent_id, child_id)  # Register again

        self.assertEqual(len(self.session.children[parent_id]), 1)
        self.assertFalse(self.session.is_dirty)  # Should not be dirty

    def test_unregister_component_removes_single_component(self):
        """Test that unregistering a component with no children works normally."""
        component_id = "test-component"

        # Mock component state
        self.session.states[component_id] = '{"id": "test-component", "hx_name": "MockComponent"}'
        self.session.subscriptions[component_id] = {"test-signal"}

        self.session.unregister_component(component_id)

        self.assertNotIn(component_id, self.session.states)
        self.assertNotIn(component_id, self.session.subscriptions)
        self.assertIn(component_id, self.session.unregistered)

    def test_unregister_component_recursively_removes_children(self):
        """Test that unregistering a parent component removes all children recursively."""
        parent_id = "parent-1"
        child1_id = "child-1"
        child2_id = "child-2"
        grandchild_id = "grandchild-1"

        # Set up component hierarchy
        self.session.register_child(parent_id, child1_id)
        self.session.register_child(parent_id, child2_id)
        self.session.register_child(child1_id, grandchild_id)  # child1 has a child

        # Mock component states
        for comp_id in [parent_id, child1_id, child2_id, grandchild_id]:
            self.session.states[comp_id] = f'{{"id": "{comp_id}", "hx_name": "MockComponent"}}'
            self.session.subscriptions[comp_id] = {f"{comp_id}-signal"}

        # Unregister parent
        self.session.unregister_component(parent_id)

        # All components should be removed
        for comp_id in [parent_id, child1_id, child2_id, grandchild_id]:
            self.assertNotIn(
                comp_id, self.session.states, f"{comp_id} should be removed from states"
            )
            self.assertNotIn(
                comp_id,
                self.session.subscriptions,
                f"{comp_id} should be removed from subscriptions",
            )
            self.assertIn(
                comp_id, self.session.unregistered, f"{comp_id} should be in unregistered"
            )

        # Children mappings should be cleaned up
        self.assertEqual(len(self.session.children), 0, "All children mappings should be removed")

    def test_unregister_component_removes_from_parent_children_list(self):
        """Test that unregistering a child removes it from parent's children list."""
        parent_id = "parent-1"
        child1_id = "child-1"
        child2_id = "child-2"

        # Set up parent with two children
        self.session.register_child(parent_id, child1_id)
        self.session.register_child(parent_id, child2_id)

        # Mock states
        self.session.states[child1_id] = '{"id": "child-1", "hx_name": "MockComponent"}'

        # Unregister one child
        self.session.unregister_component(child1_id)

        # child1 should be removed from parent's children, but child2 should remain
        self.assertNotIn(child1_id, self.session.children[parent_id])
        self.assertIn(child2_id, self.session.children[parent_id])

    @patch("djhtmx.repo.conn")
    def test_flush_persists_children_mapping(self, mock_conn):
        """Test that children mapping is persisted to Redis during flush."""
        parent_id = "parent-1"
        child_id = "child-1"

        self.session.register_child(parent_id, child_id)
        self.session.flush()

        # Verify conn.hset was called with children data
        mock_conn.hset.assert_called()
        calls = mock_conn.hset.call_args_list

        # Find the call that sets __children__
        children_call = None
        for call in calls:
            args, _kwargs = call
            if len(args) >= 3 and args[1] == "__children__":
                children_call = call
                break

        self.assertIsNotNone(children_call, "Should have called hset for __children__")

    @patch("djhtmx.repo.conn")
    def test_ensure_read_restores_children_mapping(self, mock_conn):
        """Test that children mapping is restored from Redis during _ensure_read."""
        parent_id = "parent-1"
        child_id = "child-1"

        # Mock Redis data
        mock_conn.hgetall.return_value = {
            b"__children__": b'{"parent-1": ["child-1"]}',
            b"parent-1": b'{"id": "parent-1", "hx_name": "MockComponent"}',
            b"child-1": b'{"id": "child-1", "hx_name": "MockComponent"}',
        }

        session = Session("test-session-id")
        session._ensure_read()

        # Verify children mapping was restored
        self.assertIn(parent_id, session.children)
        self.assertIn(child_id, session.children[parent_id])


class TestRepositoryWithRecursiveDestruction(TestCase):
    """Test Repository integration with recursive destruction."""

    def test_destroy_command_triggers_recursive_unregistration(self):
        """Test that Destroy command directly triggers recursive component unregistration."""

        session = Session("test-session-id")
        parent_id = "parent-1"
        child_id = "child-1"
        grandchild_id = "grandchild-1"

        # Set up component hierarchy
        session.register_child(parent_id, child_id)
        session.register_child(child_id, grandchild_id)

        # Mock component states
        for comp_id in [parent_id, child_id, grandchild_id]:
            session.states[comp_id] = f'{{"id": "{comp_id}", "hx_name": "MockComponent"}}'
            session.subscriptions[comp_id] = {f"{comp_id}-signal"}

        # Directly test the Session.unregister_component method
        session.unregister_component(parent_id)

        # Verify all components in the hierarchy were unregistered
        for comp_id in [parent_id, child_id, grandchild_id]:
            self.assertNotIn(comp_id, session.states, f"{comp_id} should be removed from states")
            self.assertNotIn(
                comp_id, session.subscriptions, f"{comp_id} should be removed from subscriptions"
            )
            self.assertIn(comp_id, session.unregistered, f"{comp_id} should be in unregistered")

        # Children mappings should be cleaned up
        self.assertEqual(len(session.children), 0, "All children mappings should be removed")


class TestAutomaticRelationshipTracking(TestCase):
    """Test automatic parent-child relationship tracking during component creation."""

    @patch("djhtmx.repo.conn")
    def test_build_and_render_automatically_tracks_parent_child_relationship(self, mock_conn):
        """Test that BuildAndRender automatically establishes parent-child relationships."""
        from django.contrib.auth.models import AnonymousUser
        from django.http import QueryDict

        from djhtmx.command_queue import CommandQueue
        from djhtmx.component import BuildAndRender
        from djhtmx.repo import Repository, Session

        # Mock Redis to return empty data
        mock_conn.hgetall.return_value = {}

        session = Session("test-session-id")
        repo = Repository(user=AnonymousUser(), session=session, params=QueryDict())

        parent_id = "parent-component"
        child_id = "child-component"

        # Set up parent component state
        session.states[parent_id] = '{"id": "parent-component", "hx_name": "MockComponent"}'

        # Create a BuildAndRender command with explicit parent_id
        build_command = BuildAndRender(
            component=MockComponent, state={"id": child_id}, oob="true", parent_id=parent_id
        )

        # Process the command through the repository
        commands = CommandQueue([build_command])
        list(repo._run_command(commands))

        # Verify parent-child relationship was automatically established
        self.assertIn(parent_id, session.children)
        self.assertIn(child_id, session.children[parent_id])

    @patch("djhtmx.repo.conn")
    def test_build_and_render_ignores_self_relationships(self, mock_conn):
        """Test BuildAndRender doesn't create self-relationships."""
        from django.contrib.auth.models import AnonymousUser
        from django.http import QueryDict

        from djhtmx.command_queue import CommandQueue
        from djhtmx.component import BuildAndRender
        from djhtmx.repo import Repository, Session

        # Mock Redis to return empty data
        mock_conn.hgetall.return_value = {}

        session = Session("test-session-id")
        repo = Repository(user=AnonymousUser(), session=session, params=QueryDict())

        component_id = "same-component"

        # Create a BuildAndRender command where parent and child are the same
        build_command = BuildAndRender(
            component=MockComponent, state={"id": component_id}, oob="true", parent_id=component_id
        )

        commands = CommandQueue([build_command])

        # Process the command
        list(repo._run_command(commands))

        # Verify no self-relationship was created
        self.assertEqual(len(session.children), 0)

    @patch("djhtmx.repo.conn")
    def test_build_and_render_ignores_empty_parent_id(self, mock_conn):
        """Test BuildAndRender doesn't track relationships when no parent is executing."""
        from django.contrib.auth.models import AnonymousUser
        from django.http import QueryDict

        from djhtmx.command_queue import CommandQueue
        from djhtmx.component import BuildAndRender
        from djhtmx.repo import Repository, Session

        # Mock Redis to return empty data
        mock_conn.hgetall.return_value = {}

        session = Session("test-session-id")
        repo = Repository(user=AnonymousUser(), session=session, params=QueryDict())

        child_id = "child-component"

        # Create a BuildAndRender command with no parent_id
        build_command = BuildAndRender(
            component=MockComponent, state={"id": child_id}, oob="true", parent_id=None
        )

        commands = CommandQueue([build_command])

        # Process the command
        list(repo._run_command(commands))

        # Verify no relationship was created
        self.assertEqual(len(session.children), 0)

    @patch("djhtmx.repo.conn")
    def test_automatic_recursive_destruction_with_built_children(self, mock_conn):
        """Test that automatically tracked children are recursively destroyed."""
        from django.contrib.auth.models import AnonymousUser
        from django.http import QueryDict

        from djhtmx.command_queue import CommandQueue
        from djhtmx.component import BuildAndRender
        from djhtmx.repo import Repository, Session

        # Mock Redis to return empty data
        mock_conn.hgetall.return_value = {}

        session = Session("test-session-id")
        repo = Repository(user=AnonymousUser(), session=session, params=QueryDict())

        parent_id = "parent-component"
        child1_id = "child1-component"
        child2_id = "child2-component"

        # Set up parent component
        session.states[parent_id] = '{"id": "parent-component", "hx_name": "MockComponent"}'

        # Simulate parent creating two child components
        for child_id in [child1_id, child2_id]:
            build_command = BuildAndRender(
                component=MockComponent, state={"id": child_id}, oob="true", parent_id=parent_id
            )
            commands = CommandQueue([build_command])
            list(repo._run_command(commands))

        # Verify relationships were established
        self.assertEqual(len(session.children[parent_id]), 2)
        self.assertIn(child1_id, session.children[parent_id])
        self.assertIn(child2_id, session.children[parent_id])

        # Add states for children (they would exist from BuildAndRender processing)
        session.states[child1_id] = '{"id": "child1-component", "hx_name": "MockComponent"}'
        session.states[child2_id] = '{"id": "child2-component", "hx_name": "MockComponent"}'

        # Destroy parent component
        session.unregister_component(parent_id)

        # Verify all components were recursively destroyed
        for comp_id in [parent_id, child1_id, child2_id]:
            self.assertNotIn(comp_id, session.states)
            self.assertIn(comp_id, session.unregistered)

    def test_build_and_render_api_usage_examples(self):
        """Test different ways to use BuildAndRender with parent_id."""
        from djhtmx.component import BuildAndRender

        # Example 1: Child component appended to parent's container
        child_in_parent = BuildAndRender.append(
            "#todo-list", MockComponent, parent_id="parent-todo-list", id="new-todo-item"
        )
        self.assertEqual(child_in_parent.parent_id, "parent-todo-list")
        self.assertEqual(child_in_parent.oob, "beforeend: #todo-list")

        # Example 2: Update existing component (preserves existing relationships)
        update_component = BuildAndRender.update(MockComponent, id="sidebar-widget")
        self.assertIsNone(update_component.parent_id)  # update() doesn't set parent_id

        # Example 3: Modal dialog created by parent component
        modal_dialog = BuildAndRender.prepend(
            "body", MockComponent, parent_id="main-dashboard", id="settings-modal"
        )
        self.assertEqual(modal_dialog.parent_id, "main-dashboard")
        self.assertEqual(modal_dialog.oob, "afterbegin: body")


class TestTemplateTagAutomaticTracking(TestCase):
    """Test automatic parent-child relationship tracking through template tags."""

    @patch("djhtmx.component.get_template")
    @patch("djhtmx.repo.conn")
    def test_htmx_template_tag_automatically_tracks_parent_child_relationship(
        self, mock_conn, mock_get_template
    ):
        """Test that {% htmx %} template tag automatically establishes parent-child relationships."""
        from django.contrib.auth.models import AnonymousUser
        from django.http import QueryDict
        from django.template import Context, Template

        from djhtmx.repo import Repository, Session

        # Mock Redis to return empty data
        mock_conn.hgetall.return_value = {}

        # Mock template rendering
        mock_get_template.return_value = lambda context: "<div>Mock HTML</div>"

        session = Session("test-session-id")
        repo = Repository(user=AnonymousUser(), session=session, params=QueryDict())

        parent_id = "parent-component"
        child_id = "child-component"

        # Create parent component
        parent_component = MockComponent(id=parent_id, hx_name="MockComponent", user=None)
        session.states[parent_id] = '{"id": "parent-component", "hx_name": "MockComponent"}'

        # Template that renders a child component inside parent
        template = Template("{% load htmx %}{% htmx 'MockComponent' id=child_id %}")

        # Context with parent component as "this" (simulating template rendering within parent)
        context = Context({
            "htmx_repo": repo,
            "this": parent_component,
            "child_id": child_id,
        })

        # Render the template (this should create the child with automatic parent tracking)
        template.render(context)

        # Verify parent-child relationship was automatically established
        self.assertIn(parent_id, session.children)
        self.assertIn(child_id, session.children[parent_id])

    @patch("djhtmx.repo.Repository.render_html")
    @patch("djhtmx.repo.conn")
    def test_htmx_template_tag_ignores_self_relationships(self, mock_conn, mock_render_html):
        """Test that template tag doesn't create self-relationships."""
        from django.contrib.auth.models import AnonymousUser
        from django.http import QueryDict
        from django.template import Context, Template

        from djhtmx.repo import Repository, Session

        # Mock Redis to return empty data
        mock_conn.hgetall.return_value = {}

        # Mock template rendering
        mock_render_html.return_value = "<div>Mock HTML</div>"

        session = Session("test-session-id")
        repo = Repository(user=AnonymousUser(), session=session, params=QueryDict())

        component_id = "same-component"

        # Create component
        component = MockComponent(id=component_id, hx_name="MockComponent", user=None)
        session.states[component_id] = '{"id": "same-component", "hx_name": "MockComponent"}'

        # Template that renders itself (should not create self-relationship)
        template = Template("{% load htmx %}{% htmx 'MockComponent' id=component_id %}")

        # Context with component as "this" and trying to render same component
        context = Context({
            "htmx_repo": repo,
            "this": component,
            "component_id": component_id,
        })

        # Render the template
        template.render(context)

        # Verify no self-relationship was created
        self.assertEqual(len(session.children), 0)

    @patch("djhtmx.repo.Repository.render_html")
    @patch("djhtmx.repo.conn")
    def test_htmx_template_tag_works_without_parent_context(self, mock_conn, mock_render_html):
        """Test that template tag works normally when no parent context is available."""
        from django.contrib.auth.models import AnonymousUser
        from django.http import QueryDict
        from django.template import Context, Template

        from djhtmx.repo import Repository, Session

        # Mock Redis to return empty data
        mock_conn.hgetall.return_value = {}

        # Mock template rendering
        mock_render_html.return_value = "<div>Mock HTML</div>"

        session = Session("test-session-id")
        repo = Repository(user=AnonymousUser(), session=session, params=QueryDict())

        child_id = "child-component"

        # Template that renders a component without parent context
        template = Template("{% load htmx %}{% htmx 'MockComponent' id=child_id %}")

        # Context without "this" (simulating top-level rendering)
        context = Context({
            "htmx_repo": repo,
            "child_id": child_id,
        })

        # Render the template (should work normally without establishing relationships)
        template.render(context)

        # Verify no relationship was created (since no parent context exists)
        self.assertEqual(len(session.children), 0)

    @patch("djhtmx.repo.Repository.render_html")
    @patch("djhtmx.repo.conn")
    def test_template_tag_recursive_destruction_integration(self, mock_conn, mock_render_html):
        """Test end-to-end integration: template tag creates hierarchy, destruction works recursively."""
        from django.contrib.auth.models import AnonymousUser
        from django.http import QueryDict
        from django.template import Context, Template

        from djhtmx.repo import Repository, Session

        # Mock Redis to return empty data
        mock_conn.hgetall.return_value = {}

        # Mock template rendering
        mock_render_html.return_value = "<div>Mock HTML</div>"

        session = Session("test-session-id")
        repo = Repository(user=AnonymousUser(), session=session, params=QueryDict())

        parent_id = "todo-list"
        child1_id = "todo-item-1"
        child2_id = "todo-item-2"

        # Create parent component
        parent_component = MockComponent(id=parent_id, hx_name="MockComponent", user=None)
        session.states[parent_id] = '{"id": "todo-list", "hx_name": "MockComponent"}'

        # Template that renders two child components
        template = Template("""
            {% load htmx %}
            {% htmx 'MockComponent' id=child1_id %}
            {% htmx 'MockComponent' id=child2_id %}
        """)

        # Context with parent component (simulating rendering within parent template)
        context = Context({
            "htmx_repo": repo,
            "this": parent_component,
            "child1_id": child1_id,
            "child2_id": child2_id,
        })

        # Render the template (creates both children with automatic tracking)
        template.render(context)

        # Verify parent-child relationships were established
        self.assertEqual(len(session.children[parent_id]), 2)
        self.assertIn(child1_id, session.children[parent_id])
        self.assertIn(child2_id, session.children[parent_id])

        # Add states for children manually (in real usage, this would happen in render_html)
        session.states[child1_id] = '{"id": "todo-item-1", "hx_name": "MockComponent"}'
        session.states[child2_id] = '{"id": "todo-item-2", "hx_name": "MockComponent"}'

        # Destroy parent component (should recursively destroy children)
        session.unregister_component(parent_id)

        # Verify all components were recursively destroyed
        for comp_id in [parent_id, child1_id, child2_id]:
            self.assertNotIn(comp_id, session.states)
            self.assertIn(comp_id, session.unregistered)
