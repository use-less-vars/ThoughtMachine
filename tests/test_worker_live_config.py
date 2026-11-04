"""W9 live config: ``config_update`` / ``config_ack`` and its runtime plumbing.

Three layers are covered, and each is asserted where it actually lives rather
than through a stand-in for the layer above it:

* the codec (:mod:`tools.workspace.worker_runtime_protocol`) -- round trip,
  missing-field refusal, the closed ``tier`` set, and the direction tables that
  make a misdirected frame a protocol error instead of a silent ignore;
* the runtime side (:func:`tools.workspace.worker_runtime_main
  ._handle_config_update`) -- one ack on every path, and the ``tier`` it reports
  matching what the process will really do at the next turn boundary;
* the supervisor handle (:class:`tools.workspace.worker_runtime_process
  .ProcessWorkerRuntime`) -- capability feature-detection off ``hello``, and a
  refusal to send a frame an older runtime would silently drop.

The end-to-end spawn path (``WORKER_RUNTIME_MODE=process``) is not exercised
here: it needs a real child process and is covered by the seam suites.
"""

import pytest

from agent.config.models import AgentConfig
from tools.workspace.worker_runtime_main import _handle_config_update
from tools.workspace.worker_runtime_process import (
    ProcessWorkerRuntime,
    WorkerRuntimeLost,
    WorkerRuntimeUnsupported,
)
from tools.workspace.worker_runtime_protocol import (
    CAPABILITY_CONFIG_UPDATE,
    CONFIG_TIER_HOT_SWAP,
    CONFIG_TIER_REQUIRES_RESPAWN,
    CONFIG_TIERS,
    DIRECTION_FROM_RUNTIME,
    DIRECTION_TO_RUNTIME,
    HELLO_CAPABILITIES,
    KINDS_BY_DIRECTION,
    MESSAGE_TYPES,
    ConfigAck,
    ConfigUpdate,
    FrameDecodeError,
    Hello,
    UnexpectedFrameError,
    check_kind,
    decode_frame,
    encode_frame,
)

#: Envelope identity used by every frame this suite builds.
_ENVELOPE = dict(worker="w1", instance=0, epoch=7)


def _make_config(**overrides):
    """Minimal real ``AgentConfig``; the tier question is about real fields."""
    base = dict(api_key="test-key", enable_logging=False, provider_config={})
    base.update(overrides)
    return AgentConfig(**base)


def _frame(message, seq=1):
    return message.to_frame(seq=seq, **_ENVELOPE)


def _round_trip(message, direction=DIRECTION_TO_RUNTIME):
    """encode -> length-stripped body -> decode, exactly as the socket does."""
    wire = encode_frame(_frame(message))
    body = wire[4:]  # 4-byte big-endian length prefix (§4.2)
    return decode_frame(body, direction=direction)


# --------------------------------------------------------------------------
# Codec
# --------------------------------------------------------------------------


class TestConfigUpdateFrame:
    def test_round_trip(self):
        message = ConfigUpdate(request_id="r-1", config={"model": "m2"})
        frame = _round_trip(message)
        assert frame["kind"] == ConfigUpdate.KIND
        assert ConfigUpdate.from_frame(frame) == message

    def test_kind_is_registered_both_ways(self):
        assert MESSAGE_TYPES[ConfigUpdate.KIND] is ConfigUpdate
        assert ConfigUpdate.KIND in KINDS_BY_DIRECTION[DIRECTION_TO_RUNTIME]
        # A supervisor-side frame must never be accepted coming the other way.
        assert ConfigUpdate.KIND not in KINDS_BY_DIRECTION[DIRECTION_FROM_RUNTIME]
        with pytest.raises(UnexpectedFrameError):
            check_kind(ConfigUpdate.KIND, DIRECTION_FROM_RUNTIME)

    def test_missing_declared_field_is_refused(self):
        frame = _frame(ConfigUpdate(request_id="r-1", config={}))
        del frame["payload"]["config"]
        with pytest.raises(FrameDecodeError):
            ConfigUpdate.from_frame(frame)

    def test_extra_payload_key_is_ignored(self):
        """§4.5 additive rule: a newer peer's extra key must not be fatal."""
        frame = _frame(ConfigUpdate(request_id="r-1", config={}))
        frame["payload"]["future_field"] = "whatever"
        assert ConfigUpdate.from_frame(frame).request_id == "r-1"


class TestConfigAckFrame:
    def test_round_trip(self):
        message = ConfigAck(
            request_id="r-1", ok=True, tier=CONFIG_TIER_HOT_SWAP, status="queued"
        )
        frame = _round_trip(message, DIRECTION_FROM_RUNTIME)
        assert frame["kind"] == ConfigAck.KIND
        assert ConfigAck.from_frame(frame) == message

    def test_kind_is_registered_both_ways(self):
        assert MESSAGE_TYPES[ConfigAck.KIND] is ConfigAck
        assert ConfigAck.KIND in KINDS_BY_DIRECTION[DIRECTION_FROM_RUNTIME]
        with pytest.raises(UnexpectedFrameError):
            check_kind(ConfigAck.KIND, DIRECTION_TO_RUNTIME)

    def test_unknown_tier_is_refused(self):
        with pytest.raises(UnexpectedFrameError):
            ConfigAck(request_id="r-1", ok=True, tier="applied", status="")

    def test_every_known_tier_is_accepted(self):
        for tier in CONFIG_TIERS:
            ack = ConfigAck(request_id="r-1", ok=True, tier=tier, status="")
            assert ack.tier == tier

    def test_missing_declared_field_is_refused(self):
        frame = _frame(
            ConfigAck(
                request_id="r-1", ok=True, tier=CONFIG_TIER_HOT_SWAP, status=""
            )
        )
        del frame["payload"]["tier"]
        with pytest.raises(FrameDecodeError):
            ConfigAck.from_frame(frame)


class TestHelloCapabilities:
    def test_old_hello_without_capabilities_still_decodes(self):
        """The compat mechanism: an older runtime's hello must still handshake."""
        frame = _frame(
            Hello(pid=1, started_at="t", runtime_version="v", epoch_seen=7)
        )
        del frame["payload"]["capabilities"]
        assert Hello.from_frame(frame).capabilities == ()

    def test_new_hello_advertises_config_update(self):
        assert CAPABILITY_CONFIG_UPDATE in HELLO_CAPABILITIES
        frame = _round_trip(
            Hello(
                pid=1,
                started_at="t",
                runtime_version="v",
                epoch_seen=7,
                capabilities=HELLO_CAPABILITIES,
            ),
            DIRECTION_FROM_RUNTIME,
        )
        assert Hello.from_frame(frame).capabilities == HELLO_CAPABILITIES


# --------------------------------------------------------------------------
# Runtime side: _handle_config_update
# --------------------------------------------------------------------------


class _Outbox:
    """Collects what the handler emitted; the real outbox writes to a socket."""

    def __init__(self):
        self.messages = []

    def put(self, message):
        self.messages.append(message)

    def ack(self):
        acks = [m for m in self.messages if isinstance(m, ConfigAck)]
        assert len(acks) == 1, f"expected exactly one config_ack, got {acks}"
        return acks[0]


class _Agent:
    def __init__(self, config):
        self.config = config
        self.queued = []

    def request_config_update(self, new_config):
        self.queued.append(new_config)


class _AgentWithoutMailbox:
    """An agent shape that predates the ``request_config_update`` mailbox."""

    request_config_update = None

    def __init__(self, config):
        self.config = config


class _Worker:
    """The parts of ``WorkerThread`` the handler touches, and nothing else."""

    def __init__(self, config_dict, *, agent=None, new_config=None, error=None):
        self._agent_config_dict = dict(config_dict)
        self._agent = agent
        self._new_config = new_config
        self._error = error
        self.built = []

    def _build_agent_config(self):
        self.built.append(dict(self._agent_config_dict))
        if self._error is not None:
            raise self._error
        return self._new_config


_ORIGINAL_CONFIG = {"provider": "anthropic", "model": "m1", "api_key": "k"}


def _update(config):
    return ConfigUpdate(request_id="r-1", config=config)


class TestHandleConfigUpdate:
    def test_no_agent_yet_stashes_and_reports_hot_swap(self):
        worker = _Worker(_ORIGINAL_CONFIG, agent=None, new_config=_make_config())
        original = worker._agent_config_dict
        outbox = _Outbox()

        _handle_config_update(worker, outbox, _update({"model": "m2"}))

        ack = outbox.ack()
        assert ack.request_id == "r-1"
        assert ack.ok is True
        assert ack.tier == CONFIG_TIER_HOT_SWAP
        assert ack.status.startswith("stashed")
        # The first turn builds the agent from the merged dict: no replay needed.
        assert worker._agent_config_dict["model"] == "m2"
        # Merged by rebinding a copy, never by mutating the original in place.
        assert original == _ORIGINAL_CONFIG

    def test_provider_type_is_aliased_to_the_injected_name(self):
        """``AgentConfig.provider_type`` vs the executor's ``provider`` key."""
        worker = _Worker(_ORIGINAL_CONFIG, agent=None, new_config=_make_config())
        _handle_config_update(
            worker, _Outbox(), _update({"provider_type": "openai"})
        )
        assert worker._agent_config_dict["provider"] == "openai"
        assert "provider_type" not in worker._agent_config_dict

    def test_hot_swappable_change_queues_and_reports_hot_swap(self):
        old = _make_config()
        new = _make_config(enable_logging=True)
        agent = _Agent(old)
        worker = _Worker(_ORIGINAL_CONFIG, agent=agent, new_config=new)
        outbox = _Outbox()

        _handle_config_update(worker, outbox, _update({"enable_logging": True}))

        ack = outbox.ack()
        assert ack.ok is True
        assert ack.tier == CONFIG_TIER_HOT_SWAP
        assert "rebuilds itself" not in ack.status
        assert agent.queued == [new]

    def test_respawn_required_change_is_accepted_but_reports_the_tier(self):
        old = _make_config(model="m1")
        new = _make_config(model="m2")
        agent = _Agent(old)
        worker = _Worker(_ORIGINAL_CONFIG, agent=agent, new_config=new)
        outbox = _Outbox()

        _handle_config_update(worker, outbox, _update({"model": "m2"}))

        ack = outbox.ack()
        assert ack.ok is True
        assert ack.tier == CONFIG_TIER_REQUIRES_RESPAWN
        assert "rebuilds itself" in ack.status
        assert agent.queued == [new]

    def test_missing_build_hook_is_answered_not_raised(self):
        worker = object()  # no _build_agent_config at all
        outbox = _Outbox()

        _handle_config_update(worker, outbox, _update({"model": "m2"}))

        ack = outbox.ack()
        assert ack.ok is False
        assert ack.tier == CONFIG_TIER_REQUIRES_RESPAWN
        assert "_build_agent_config" in ack.status

    def test_build_failure_is_answered(self):
        worker = _Worker(
            _ORIGINAL_CONFIG, new_config=_make_config(), error=ValueError("bad key")
        )
        outbox = _Outbox()

        _handle_config_update(worker, outbox, _update({"api_key": ""}))

        ack = outbox.ack()
        assert ack.ok is False
        assert "ValueError: bad key" in ack.status

    def test_invalid_config_is_answered(self):
        worker = _Worker(_ORIGINAL_CONFIG, new_config=None)
        outbox = _Outbox()

        _handle_config_update(worker, outbox, _update({"model": ""}))

        ack = outbox.ack()
        assert ack.ok is False
        assert "invalid agent_config" in ack.status

    def test_agent_without_a_mailbox_is_answered(self):
        worker = _Worker(
            _ORIGINAL_CONFIG,
            agent=_AgentWithoutMailbox(_make_config()),
            new_config=_make_config(),
        )
        outbox = _Outbox()

        _handle_config_update(worker, outbox, _update({"model": "m2"}))

        ack = outbox.ack()
        assert ack.ok is False
        assert "request_config_update" in ack.status

    def test_unclassifiable_change_is_answered(self):
        """A config the tier function cannot read is refused, not guessed at."""
        worker = _Worker(_ORIGINAL_CONFIG, agent=_Agent(None), new_config=object())
        outbox = _Outbox()

        _handle_config_update(worker, outbox, _update({"model": "m2"}))

        ack = outbox.ack()
        assert ack.ok is False
        assert ack.status.startswith("cannot classify the change")


# --------------------------------------------------------------------------
# Supervisor side: capability-gated update_config
# --------------------------------------------------------------------------


def _runtime():
    """A handle that was never ``start()``-ed: no socket, no child (§3.3)."""
    return ProcessWorkerRuntime(
        {"worker": "w1", "instance": 0, "epoch": 7, "worker_kwargs": {}}
    )


def _hello_frame(*, capabilities=None):
    kwargs = {} if capabilities is None else {"capabilities": capabilities}
    return _frame(
        Hello(
            pid=1234, started_at="t", runtime_version="v", epoch_seen=7, **kwargs
        )
    )


class TestCapabilityGate:
    def test_no_capabilities_before_hello(self):
        runtime = _runtime()
        assert runtime.capabilities == frozenset()
        assert runtime.supports(CAPABILITY_CONFIG_UPDATE) is False

    def test_update_config_refuses_an_old_runtime(self):
        with pytest.raises(WorkerRuntimeUnsupported) as excinfo:
            _runtime().update_config({"model": "m2"})
        assert CAPABILITY_CONFIG_UPDATE in str(excinfo.value)

    def test_hello_without_capabilities_leaves_the_gate_closed(self):
        runtime = _runtime()
        runtime._route(_hello_frame())
        assert runtime.supports(CAPABILITY_CONFIG_UPDATE) is False
        with pytest.raises(WorkerRuntimeUnsupported):
            runtime.update_config({"model": "m2"})

    def test_hello_with_capabilities_opens_the_gate(self):
        runtime = _runtime()
        runtime._route(_hello_frame(capabilities=HELLO_CAPABILITIES))
        assert runtime.capabilities == frozenset(HELLO_CAPABILITIES)
        assert runtime.supports(CAPABILITY_CONFIG_UPDATE) is True
        assert runtime.hello.capabilities == HELLO_CAPABILITIES

    def test_update_config_past_the_gate_needs_a_live_runtime(self):
        """Capable but never started: the failure is "gone", not "unsupported"."""
        runtime = _runtime()
        runtime._route(_hello_frame(capabilities=HELLO_CAPABILITIES))
        with pytest.raises(WorkerRuntimeLost):
            runtime.update_config({"model": "m2"})

    def test_config_ack_is_routed_to_its_waiter(self):
        runtime = _runtime()
        runtime._route(_hello_frame(capabilities=HELLO_CAPABILITIES))
        slot = runtime._register_ack("r-1")
        runtime._route(
            _frame(
                ConfigAck(
                    request_id="r-1",
                    ok=False,
                    tier=CONFIG_TIER_REQUIRES_RESPAWN,
                    status="invalid agent_config",
                )
            )
        )
        assert slot.message is not None
        assert slot.message.tier == CONFIG_TIER_REQUIRES_RESPAWN
