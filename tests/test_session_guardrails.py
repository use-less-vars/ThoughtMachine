"""
Tests for Session.__setattr__ guardrail that prevents user_history
from being replaced with a plain list.
"""
from session.models import Session, ObservableList


class TestSessionUserHistoryGuardrail:
    """Session.__setattr__ must auto-wrap plain-list assignments to user_history."""

    def test_user_history_starts_as_observable_list(self):
        """A freshly created Session has an ObservableList for user_history."""
        session = Session()
        assert isinstance(session.user_history, ObservableList), (
            f"Expected ObservableList, got {type(session.user_history).__name__}"
        )

    def test_assign_plain_list_auto_wraps(self):
        """Assigning a plain list to session.user_history auto-wraps it."""
        session = Session()
        original = session.user_history

        plain = [{"role": "user", "content": "Hello"}]
        session.user_history = plain

        # Must still be ObservableList
        assert isinstance(session.user_history, ObservableList), (
            f"Expected ObservableList after plain-list assignment, "
            f"got {type(session.user_history).__name__}"
        )
        # Content must be preserved
        assert list(session.user_history) == plain, (
            f"Expected content {plain}, got {list(session.user_history)}"
        )

    def test_assign_empty_plain_list(self):
        """Assigning an empty plain list also gets wrapped."""
        session = Session()
        session.user_history = []
        assert isinstance(session.user_history, ObservableList)
        assert list(session.user_history) == []

    def test_assign_observable_list_preserved(self):
        """Assigning an ObservableList directly keeps it as-is (no re-wrap)."""
        session = Session()
        olist = ObservableList([{"role": "user", "content": "test"}])
        session.user_history = olist
        # Should be the *same* object (not a re-wrap)
        assert session.user_history is olist

    def test_content_preserved_after_multiple_assignments(self):
        """Multiple plain-list assignments — content survives each wrap."""
        session = Session()
        data1 = [{"role": "user", "content": "First"}]
        data2 = [{"role": "assistant", "content": "Second"}]

        session.user_history = data1
        assert list(session.user_history) == data1

        session.user_history = data2
        assert list(session.user_history) == data2

    def test_conversation_version_not_incremented_by_plain_assign(self):
        """Plain-list assignment auto-wraps but does NOT bump conversation_version."""
        session = Session()
        # __post_init__ may bump the version, so grab the value after construction
        before = session._conversation_version
        session.user_history = [{"role": "user", "content": "test"}]
        # The version should not have changed (the guardrail uses object.__setattr__
        # which skips ObservableList.__setitem__)
        assert session._conversation_version == before, (
            f"Expected version {before}, got {session._conversation_version}"
        )

    def test_callback_preserved_on_wrap(self):
        """After auto-wrap, the ObservableList's callback points to the session method."""
        session = Session()
        session.user_history = [{"role": "user", "content": "hello"}]
        assert session.user_history.callback is not None
        # The callback should be bound to the session's _on_conversation_changed
        assert session.user_history.callback.__self__ is session, (
            "Callback should be bound to the same session instance"
        )
