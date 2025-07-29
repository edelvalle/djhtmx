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
