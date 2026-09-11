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


class TestSessionConstructionAwareGuard:
    """The user_history guard must distinguish construction-time wraps from live ones.

    Construction-time plain-list wraps are expected and logged at DEBUG; only a
    post-construction (live) plain-list assignment keeps the WARNING.
    """

    @staticmethod
    def _recording_log(records):
        """Build a stand-in for session.models.log that records (level, tag, message)."""
        def _record(level, tag, message, *args, **kwargs):
            records.append((level, tag, message))
        return _record

    def _core_history_warnings(self, records):
        return [r for r in records if r[0] == "WARNING" and r[1] == "core.history"]

    def test_construction_default_is_silent(self, monkeypatch):
        """Constructing Session() with the default list logs no WARNING."""
        records = []
        monkeypatch.setattr("session.models.log", self._recording_log(records))
        session = Session()
        assert self._core_history_warnings(records) == []
        assert isinstance(session.user_history, ObservableList)
        assert list(session.user_history) == []

    def test_construction_with_plain_list_is_silent(self, monkeypatch):
        """Passing a plain list to the constructor logs no WARNING."""
        records = []
        monkeypatch.setattr("session.models.log", self._recording_log(records))
        plain = [{"role": "user", "content": "hello"}]
        session = Session(user_history=plain)
        assert self._core_history_warnings(records) == []
        assert isinstance(session.user_history, ObservableList)
        assert list(session.user_history) == plain

    def test_from_persistable_dict_plain_list_is_silent(self, monkeypatch):
        """Reconstruction via from_persistable_dict (plain list) logs no WARNING."""
        records = []
        monkeypatch.setattr("session.models.log", self._recording_log(records))
        data = Session().to_persistable_dict()
        data["user_history"] = [{"role": "user", "content": "hello"}]
        session = Session.from_persistable_dict(data)
        assert self._core_history_warnings(records) == []
        assert isinstance(session.user_history, ObservableList)
        assert len(session.user_history) == 1
        assert session.user_history[0]["content"] == "hello"

    def test_live_plain_list_assignment_still_warns(self, monkeypatch):
        """A post-construction plain-list assignment logs exactly one WARNING."""
        session = Session()
        records = []
        monkeypatch.setattr("session.models.log", self._recording_log(records))
        payload = [{"role": "user", "content": "hello"}]
        session.user_history = payload
        warnings = self._core_history_warnings(records)
        assert len(warnings) == 1, f"Expected exactly one WARNING, got {warnings}"
        assert "Plain list assignment to user_history intercepted" in warnings[0][2]
        assert isinstance(session.user_history, ObservableList)
        assert list(session.user_history) == payload
        assert session.user_history.callback is not None
        assert session.user_history.callback.__self__ is session
