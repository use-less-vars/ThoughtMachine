"""
``ConfigService`` validation tests.

Covers the two previously vacuous validation paths in ``agent/config/service.py``:

* ``ConfigService.validate()`` used to ``return True`` unconditionally.
* ``ConfigService.set(..., validate=True)`` / ``update(..., validate=True)``
  accepted the flag and then ignored it entirely.
"""
import pytest
from pydantic import ValidationError

from agent.config import AgentConfig
from agent.config.service import ConfigService, create_agent_config_service


INVALID_MAX_TURNS = "not-an-int"


def _valid_config():
    """The canonical live-config shape: an ``AgentConfig`` dump minus api_key."""
    return AgentConfig().model_dump(exclude={"api_key"}, exclude_none=True)


def _service(tmp_path, config=None):
    """A ConfigService whose live config is the given (valid) mapping."""
    return ConfigService(str(tmp_path / "agent_config.json"), config or _valid_config())


class TestConfigServiceValidate:
    """``ConfigService.validate()`` must delegate to ``AgentConfig``."""

    def test_config_service_validate_delegates(self, tmp_path):
        """validate() returns True for a valid live config (delegation, not vacuity)."""
        service = _service(tmp_path)
        assert service.validate() is True

    def test_config_service_validate_delegates_to_agent_config(self, tmp_path, monkeypatch):
        """validate() really calls AgentConfig.model_validate on get_all()."""
        service = _service(tmp_path)
        seen = {}
        real = AgentConfig.model_validate

        def spy(cls, obj, *args, **kwargs):
            seen["obj"] = obj
            return real(obj, *args, **kwargs)

        monkeypatch.setattr(AgentConfig, "model_validate", classmethod(spy))
        assert service.validate() is True
        assert seen["obj"] == service.get_all()

    def test_config_service_validate_no_longer_vacuous(self, tmp_path):
        """An invalid live config must raise, not silently report True.

        NOTE: no prior test asserted ``ConfigService.validate() is True`` on an
        invalid config (grepped tests/), so there was nothing to invert - this
        test proves the vacuity is gone instead of flipping an existing one.
        """
        service = _service(tmp_path)
        service.set("max_turns", INVALID_MAX_TURNS, notify=False, save=False, validate=False)
        assert service.get("max_turns") == INVALID_MAX_TURNS  # bad value is live

        with pytest.raises(ValidationError):
            service.validate()

    def test_validate_rejects_unknown_keys(self, tmp_path):
        """AgentConfig forbids extras, so unknown keys are rejected too."""
        service = _service(tmp_path)
        service.set("totally_unknown_key", 1, notify=False, save=False, validate=False)
        with pytest.raises(ValidationError):
            service.validate()


class TestSetValidateFlagHonoured:
    """``validate=True`` on writes must be honoured."""

    def test_set_validate_flag_honoured(self, tmp_path):
        """set(validate=True) raises on a value the schema rejects.

        Mutation is atomic: validation runs against a *candidate* mapping before
        the live dict is touched, so the rejected value is never applied, never
        announced to listeners and never saved.
        """
        service = _service(tmp_path)
        before = service.get_all()
        notifications = []
        service.add_listener(lambda *a: notifications.append(a))

        with pytest.raises(ValidationError):
            service.set("max_turns", INVALID_MAX_TURNS)

        assert service.get("max_turns") == before["max_turns"]
        assert service.get_all() == before
        assert notifications == []

    def test_set_validate_flag_can_be_disabled(self, tmp_path):
        """validate=False still bypasses validation (escape hatch preserved)."""
        service = _service(tmp_path)
        service.set("max_turns", INVALID_MAX_TURNS, notify=False, save=False, validate=False)
        assert service.get("max_turns") == INVALID_MAX_TURNS

    def test_set_valid_value_passes(self, tmp_path):
        """A schema-valid write is still applied when validate=True."""
        service = _service(tmp_path)
        service.set("max_turns", 42, notify=False, save=False)
        assert service.get("max_turns") == 42

    def test_update_validate_flag_honoured(self, tmp_path):
        """update(validate=True) raises and applies nothing (all-or-nothing)."""
        service = _service(tmp_path)
        before = service.get_all()

        with pytest.raises(ValidationError):
            service.update({"max_turns": INVALID_MAX_TURNS, "temperature": 0.42})

        assert service.get_all() == before

    def test_update_validate_flag_can_be_disabled(self, tmp_path):
        """update(validate=False) still bypasses validation."""
        service = _service(tmp_path)
        service.update({"max_turns": INVALID_MAX_TURNS}, notify=False, save=False, validate=False)
        assert service.get("max_turns") == INVALID_MAX_TURNS

    def test_create_agent_config_service_validates_clean(self, tmp_path):
        """The production factory produces a config that validates."""
        service = create_agent_config_service(str(tmp_path / "agent_config.json"))
        assert service.validate() is True
