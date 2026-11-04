# tools/workspace/worker_runtime_protocol.py
"""Wire protocol for a worker that may run in its own OS process (P3, stage S0).

`docs/design-worker-runtime-process.md` §4 defines the contract implemented
here; §10 makes S0 the stage that ships the *messages* and the *façade* while
every call is still in-process. Nothing in this module opens a socket, starts
a thread or reads the clock: it is a pure codec plus the typed messages from
§4.3 (supervisor → runtime) and §4.4 (runtime → supervisor).

Framing (§4.2)
--------------
``4-byte big-endian unsigned length`` followed by that many bytes of UTF-8
JSON. The length counts the JSON body only, so an exact-:data:`MAX_FRAME_BYTES`
body is legal and one byte more is not. Envelope:

    {"v": 1, "kind": "<str>", "seq": <int>, "worker": "<name>",
     "instance": <int>, "epoch": <int>, "payload": { ... }}

``worker`` / ``instance`` / ``epoch`` mirror ``CommandFence`` for the same
reason it carries them: a frame addressed to a different incarnation must be
identifiable without trusting the socket's provenance. Identification is only
worth having if something acts on it, so :class:`FrameDecoder` accepts an
optional :class:`ExpectedFence` and, when one is supplied, **drops** a frame
whose three axes disagree with it (§4.7 row 7). Dropping is not a protocol
error and does not touch the connection: a dead generation's trailing
``heartbeat`` is garbage to discard, not a reason to ``SIGKILL`` a healthy
runtime. Each drop is counted (:attr:`FrameDecoder.fence_rejects`) and passed
to an optional callback, because the command channel's rule -- a fence
mismatch always leaves evidence -- is the standard being matched, and the
evidence on a socket is a counter plus a log line rather than a quarantined
file. The check is opt-in: with no :class:`ExpectedFence` the decoder behaves
exactly as before.

``seq`` is **per-direction, per-connection**, starts at 1 and strictly
increases (§4.2, §4.8). It is therefore modelled by two explicit objects --
:class:`FrameEmitter` on the send side and :class:`SeqTracker` on the receive
side, one of each per direction -- and never by one counter shared between
the directions: a single counter would make a supervisor's ``dispatch_query``
compete with the runtime's ``reply`` for the same numbers, and the two
directions have independent restart points. Contiguity is *not* required:
§4.6 lets the outbox coalesce (drop-oldest) already-sequenced frames, so a gap
is legal while a repeat or a step backwards is a desync (§4.7).

Versioning (§4.5) and unknown kinds (§4.7)
------------------------------------------
Two deliberately opposite rules, both enforced here:

* unknown **extra payload keys inside a known ``kind``** are *ignored* --
  additive payload keys do not bump ``v``;
* an unknown **``kind``** is *never* ignored -- a stream we cannot interpret
  may be mid-frame, so it is a protocol error.

``v`` is an integer and only a breaking change bumps it; a frame whose ``v``
is a well-formed integer this build does not speak is refused as an
unexpected value (the supervisor answers ``unsupported_version`` and
escalates to ``shutdown``, §4.5) rather than parsed optimistically.

Three error classes
-------------------
:class:`WorkerRuntimeProtocolError` is the base every §4.7 violation raises.

Its two *decode* subclasses separate the two questions a receiver asks:

* :class:`FrameDecodeError` -- the *stream* is malformed: zero-length or
  over-limit length prefix, invalid UTF-8, invalid JSON, a non-object body, a
  missing or mistyped ``v`` / ``kind`` / ``seq``, or ``seq`` that did not
  advance. Nothing can be done with the connection after one of these.
* :class:`UnexpectedFrameError` -- the frame parsed but carries a value the
  receiver cannot accept: an unknown ``kind``, a ``kind`` from the other
  direction, an unsupported ``v``, a ``control.action`` or
  ``cancel_ack.phase`` outside its closed set.

:class:`FrameEncodeError` is the third, on the *send* side, for a frame this
process cannot legally put on the wire -- malformed, an unsupported ``v``,
over the size ceiling, or not JSON-serialisable. It deliberately does NOT
derive from :class:`FrameDecodeError`: a writer reporting "I could not read
this" sends the reader to the wrong end of the socket. All three derive from
the base, so ``except WorkerRuntimeProtocolError`` still catches every one.

Every one of them maps to "close → SIGKILL → mark ``lost{protocol_error}``"
(§4.7); the split exists so the log line and the tests can tell "I could not
read this" from "I read it and refused it".
"""

from __future__ import annotations

import json
import struct
import threading
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
    Type,
)

#: ``v`` in every envelope (§4.5). An integer; only a BREAKING change bumps
#: it, because additive payload keys must stay readable by older receivers.
PROTOCOL_VERSION = 1

#: Hard ceiling on the JSON body of one frame (§4.2), excluding the length
#: prefix. Exactly this many bytes is accepted; one more is a protocol error.
MAX_FRAME_BYTES = 1024 * 1024

#: Width of the big-endian unsigned length prefix (§4.2).
LENGTH_PREFIX_BYTES = 4

_LENGTH_PREFIX = struct.Struct(">I")

#: Envelope keys §4.7 refuses a frame for missing. ``payload`` is deliberately
#: absent from this list: §4.7 names only these three, so a frame without a
#: payload is read as an empty one rather than rejected.
REQUIRED_ENVELOPE_KEYS: Tuple[str, ...] = ("v", "kind", "seq")

#: Supervisor → runtime (§4.3).
DIRECTION_TO_RUNTIME = "supervisor_to_runtime"

#: Runtime → supervisor (§4.4).
DIRECTION_FROM_RUNTIME = "runtime_to_supervisor"

DIRECTIONS: Tuple[str, ...] = (DIRECTION_TO_RUNTIME, DIRECTION_FROM_RUNTIME)

#: Closed set of ``control.action`` values (§4.3).
CONTROL_ACTIONS: Tuple[str, ...] = ("stop", "pause", "resume", "kill")

#: Closed ladder of ``cancel_ack.phase`` values (§5.4).
CANCEL_ACK_PHASES: Tuple[str, ...] = ("accepted", "observed", "completed", "refused")

#: ``config_ack.tier``: the change is applied at the next turn boundary without
#: rebuilding the provider (§4.9, W9).
CONFIG_TIER_HOT_SWAP = "hot_swap"

#: ``config_ack.tier``: the change cannot be applied to the running agent, so the
#: honest answer is that a fresh runtime is needed (§4.9, W9).
CONFIG_TIER_REQUIRES_RESPAWN = "requires_respawn"

#: Closed set of ``config_ack.tier`` values (§4.9).
CONFIG_TIERS: Tuple[str, ...] = (CONFIG_TIER_HOT_SWAP, CONFIG_TIER_REQUIRES_RESPAWN)

#: The ``hello.capabilities`` value that announces a runtime can take a
#: ``config_update`` frame at all (§4.5). A runtime that does not advertise it
#: is not broken -- it predates the frame -- so the supervisor feature-detects
#: this name instead of assuming, and a frame it would ignore is never sent.
CAPABILITY_CONFIG_UPDATE = "config_update"

#: Every capability name this build understands.
HELLO_CAPABILITIES: Tuple[str, ...] = (CAPABILITY_CONFIG_UPDATE,)

#: The lifecycle reason set a ``cancel`` may carry (§4.3). Deliberately NOT
#: enforced: a reason is a diagnostic string, not control flow, and new
#: ceilings / budgets are additive (§4.5). An unknown reason is still acted on.
CANCEL_REASONS: Tuple[str, ...] = (
    "operator_kill",
    "ceiling",
    "worker_budget",
    "tool_call_budget",
    "ttl_expired",
    "detached_ttl",
)


class WorkerRuntimeProtocolError(Exception):
    """Base for every §4.7 protocol violation.

    Carries ``decoded_frames`` because a decoder failure is batch-shaped: one
    ``feed`` call can complete several frames before the frame that fails, and
    those frames were real -- their ``seq`` values are consumed -- even though
    the call raises instead of returning them. Empty for every error raised
    anywhere other than :meth:`FrameDecoder.feed`.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        #: Frames fully decoded earlier in the same ``feed`` call as the frame
        #: that failed. Attached by the decoder, never by the parser.
        self.decoded_frames: Tuple[Dict[str, Any], ...] = ()


class FrameDecodeError(WorkerRuntimeProtocolError):
    """The byte stream cannot be read as a frame at all ("malformed stream")."""


class UnexpectedFrameError(WorkerRuntimeProtocolError):
    """The frame parsed but carries a value this receiver cannot accept."""


class FrameEncodeError(WorkerRuntimeProtocolError):
    """A frame this process cannot legally write ("writer-side bug")."""


def _check_direction(direction: str) -> str:
    """Return *direction* if it names one of the two directions, else raise.

    A bad direction is a programming error at the call site, not something a
    peer can send, so it raises ``ValueError`` and not a protocol error.
    """
    if direction not in DIRECTIONS:
        raise ValueError(
            f"direction must be one of {DIRECTIONS}, got {direction!r}"
        )
    return direction


def _check_body_length(
    length: int, error: Type[WorkerRuntimeProtocolError] = FrameDecodeError
) -> int:
    """Gate the length prefix / body size (§4.2, §4.7).

    Checked as soon as the 4 prefix bytes arrive and again before parsing a
    body, so an over-limit prefix can never make the decoder wait for (or
    allocate) a body that will never be legal.

    *error* names the class the refusal belongs to: the same arithmetic is a
    malformed stream when it arrives from the peer and the writer's own bug
    when it is our frame, and the caller knows which end of the socket it is
    on.
    """
    if length == 0:
        raise error("zero-length frame is a protocol error (§4.7)")
    if length > MAX_FRAME_BYTES:
        raise error(
            f"frame length {length} exceeds the {MAX_FRAME_BYTES}-byte maximum (§4.2)"
        )
    return length


def _coerce(kind: str, name: str, value: Any, expected: Any) -> Any:
    """Type-check one payload field, tolerating ``int`` for a float field.

    ``type(value) is allowed`` rather than ``isinstance``, for the reason
    ``CommandFence._FENCE_FIELDS`` gives: ``True`` IS an ``int``, so a bool
    must not satisfy an integer field. A JSON number arrives as ``int`` when
    its literal had no fractional part, so a declared ``float`` field accepts
    that and normalises instead of failing on syntax.
    """
    allowed = expected if isinstance(expected, tuple) else (expected,)
    if float in allowed and type(value) is int:
        return float(value)
    if type(value) not in allowed:
        names = "/".join(t.__name__ for t in allowed)
        raise UnexpectedFrameError(
            f"{kind}.{name} must be {names}, got {type(value).__name__}"
        )
    return value


@dataclass(frozen=True)
class Message:
    """One ``kind`` of frame as a typed value (§4.3 / §4.4).

    Subclasses declare their payload fields twice on purpose: once as
    dataclass annotations (so the constructor is real) and once in
    ``_FIELDS`` (so parse and serialise share one ordered, typed list, and a
    field can never be silently dropped from either). Reading a frame builds
    the instance BY KEYWORD, so a future reorder of ``_FIELDS`` cannot swap
    two fields of the same type.
    """

    KIND: ClassVar[str] = ""
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = ()
    #: Declared fields a peer may OMIT (§4.5 additive rule). Absent means "the
    #: peer was built before this field existed", so the dataclass default is
    #: used instead of refusing the frame -- which is the only way an addition
    #: can be additive for a receiver as well as for a sender.
    _OPTIONAL: ClassVar[Tuple[str, ...]] = ()

    def to_payload(self) -> Dict[str, Any]:
        """The declared payload fields, in declaration order."""
        return {name: getattr(self, name) for name, _ in self._FIELDS}

    def to_frame(
        self,
        *,
        seq: int,
        worker: str,
        instance: int,
        epoch: int,
    ) -> Dict[str, Any]:
        """This message as a complete envelope, with ``seq`` supplied.

        ``seq`` is a parameter rather than a field because it belongs to the
        connection's direction, not to the message: the same value may be
        re-sent at a new ``seq`` after a reconnect, and one counter per
        direction (§4.2) is what :class:`FrameEmitter` owns.
        """
        return {
            "v": PROTOCOL_VERSION,
            "kind": self.KIND,
            "seq": seq,
            "worker": worker,
            "instance": instance,
            "epoch": epoch,
            "payload": self.to_payload(),
        }

    @classmethod
    def from_frame(cls, frame: Dict[str, Any]) -> "Message":
        """Rebuild this message from a decoded frame.

        Unknown EXTRA payload keys are ignored (§4.5) -- that is the whole
        point of the additive-key rule -- so only the declared fields are
        read and a newer peer's additions survive as no-ops rather than as
        errors. A *missing* declared field is malformed: a frame we cannot
        fully read is refused, never defaulted.
        """
        if not isinstance(frame, dict):
            raise FrameDecodeError(
                f"frame must be a JSON object, got {type(frame).__name__}"
            )
        kind = frame.get("kind")
        if kind != cls.KIND:
            raise UnexpectedFrameError(
                f"frame kind {kind!r} cannot be read as {cls.KIND!r}"
            )
        payload = frame.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise FrameDecodeError(
                f"{cls.KIND} payload must be a JSON object, got {type(payload).__name__}"
            )
        values: Dict[str, Any] = {}
        for name, expected in cls._FIELDS:
            if name not in payload:
                if name in cls._OPTIONAL:
                    continue
                raise FrameDecodeError(f"{cls.KIND} payload is missing {name!r}")
            values[name] = _coerce(cls.KIND, name, payload[name], expected)
        return cls(**values)


# --------------------------------------------------------------------------
# Supervisor -> runtime (§4.3)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchQuery(Message):
    """``dispatch_query`` -- replaces ``_input_queue.put((query_id, query, q))``.

    ``reply_q`` becomes ``pending[query_id]`` on the supervisor side (§4.3);
    the queue itself is unserialisable and cannot cross the boundary (D9).
    """

    KIND: ClassVar[str] = "dispatch_query"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("query_id", str),
        ("query", str),
        ("timeout_seconds", float),
    )

    query_id: str
    query: str
    timeout_seconds: float


@dataclass(frozen=True)
class Cancel(Message):
    """``cancel`` -- a cancellation request, with its attribution (§4.3, §5.3).

    A frame rather than a signal because a signal carries no attribution and
    lands at an arbitrary bytecode boundary (§5.3). ``reason`` is not
    validated against :data:`CANCEL_REASONS` -- see that constant.
    """

    KIND: ClassVar[str] = "cancel"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("request_id", str),
        ("reason", str),
        ("deadline_ms", int),
    )

    request_id: str
    reason: str
    deadline_ms: int


@dataclass(frozen=True)
class Control(Message):
    """``control`` -- stop / pause / resume / kill (§4.3).

    ``stop`` means **detach**, not terminate (§4.3 with §11.2 rule 7):
    ``detach()`` must not set ``_stop_event``.
    """

    KIND: ClassVar[str] = "control"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("request_id", str),
        ("action", str),
    )

    request_id: str
    action: str

    def __post_init__(self) -> None:
        if self.action not in CONTROL_ACTIONS:
            raise UnexpectedFrameError(
                f"control.action must be one of {CONTROL_ACTIONS}, got {self.action!r}"
            )


@dataclass(frozen=True)
class Shutdown(Message):
    """``shutdown`` -- the supervisor is exiting; not an error condition (§4.3)."""

    KIND: ClassVar[str] = "shutdown"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (("grace_ms", int),)

    grace_ms: int


@dataclass(frozen=True)
class ConfigUpdate(Message):
    """``config_update`` -- push a new agent config to a running runtime (§4.9).

    ``config`` is a JSON object of ``AgentConfig`` field names and values -- the
    same shape the supervisor already injects into a worker at spawn. It is a
    patch, not a whole config: a runtime merges it onto the config it was born
    with, so a supervisor that does not know every field cannot silently erase
    one it never heard of.
    """

    KIND: ClassVar[str] = "config_update"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("request_id", str),
        ("config", dict),
    )

    request_id: str
    config: Dict[str, Any]


@dataclass(frozen=True)
class Ping(Message):
    """``ping`` -- only used when the socket is otherwise silent (§4.3)."""

    KIND: ClassVar[str] = "ping"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (("nonce", int),)

    nonce: int


# --------------------------------------------------------------------------
# Runtime -> supervisor (§4.4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Hello(Message):
    """``hello`` -- the runtime's first frame; asserts pid and epoch (§4.4).

    ``capabilities`` is OPTIONAL (§4.5): a runtime built before W9 sends a hello
    without it, and that runtime must still handshake -- feature detection is
    what lets the supervisor notice it can only be driven the old way. It is
    therefore a field of this frame rather than a separate kind, so the one
    handshake stays one round trip.
    """

    KIND: ClassVar[str] = "hello"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("pid", int),
        ("started_at", str),
        ("runtime_version", str),
        ("epoch_seen", int),
        ("capabilities", list),
    )
    _OPTIONAL: ClassVar[Tuple[str, ...]] = ("capabilities",)

    pid: int
    started_at: str
    runtime_version: str
    epoch_seen: int
    #: Names from :data:`HELLO_CAPABILITIES`. A tuple so a received hello is
    #: hashable and comparable; the wire form is a JSON array (§4.5).
    capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        caps = self.capabilities
        if not isinstance(caps, tuple):
            object.__setattr__(self, "capabilities", tuple(caps))

    def to_payload(self) -> Dict[str, Any]:
        """Emit ``capabilities`` as a JSON array, not as the internal tuple.

        The declared wire type is ``list`` (§4.5) and :meth:`from_frame`
        enforces it, so an envelope that has not yet been through
        ``json.dumps`` -- ``emit()`` returns exactly that -- must already carry
        an array. A tuple here would serialise fine and then fail to read back
        from any path that skipped the encoder.
        """
        payload = super().to_payload()
        payload["capabilities"] = list(self.capabilities)
        return payload


@dataclass(frozen=True)
class QueryAccepted(Message):
    """``query_accepted`` -- lets the supervisor start its wait clock (§4.4)."""

    KIND: ClassVar[str] = "query_accepted"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (("query_id", str),)

    query_id: str


@dataclass(frozen=True)
class Reply(Message):
    """``reply`` -- correlates on ``query_id``, never on arrival order (§4.8).

    ``envelope`` is the same JSON envelope ``deliver_query_and_block``
    normalises today, carried through unchanged (§4.4).
    """

    KIND: ClassVar[str] = "reply"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("query_id", str),
        ("ok", bool),
        ("envelope", dict),
        ("elapsed_seconds", float),
    )

    query_id: str
    ok: bool
    envelope: Dict[str, Any]
    elapsed_seconds: float


@dataclass(frozen=True)
class Event(Message):
    """``event`` -- what ``_publish_event`` / ``_publish_global_event`` send (§4.4)."""

    KIND: ClassVar[str] = "event"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("event_type", str),
        ("data", dict),
    )

    event_type: str
    data: Dict[str, Any]


@dataclass(frozen=True)
class Heartbeat(Message):
    """``heartbeat`` -- keeps ``HEARTBEAT_STALE_AFTER_S`` semantics (§4.4).

    ``at`` is the ISO-8601 string ``WorkerThread.last_heartbeat`` already
    holds, so the supervisor's age comparison keeps working unchanged.
    """

    KIND: ClassVar[str] = "heartbeat"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("at", str),
        ("status", str),
        ("current_context_tokens", int),
        ("max_context_tokens", int),
    )

    at: str
    status: str
    current_context_tokens: int
    max_context_tokens: int


@dataclass(frozen=True)
class WorkerState(Message):
    """``worker_state`` -- field names match ``status.json`` verbatim (§4.4).

    Verbatim so the supervisor can mirror it onto the existing file and the
    existing readers keep working.
    """

    KIND: ClassVar[str] = "worker_state"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("status", str),
        ("current_task", (str, type(None))),
        ("error", (str, type(None))),
        ("paused_manually", bool),
    )

    status: str
    current_task: Optional[str]
    error: Optional[str]
    paused_manually: bool


@dataclass(frozen=True)
class CancelAck(Message):
    """``cancel_ack`` -- one rung of the ack ladder (§4.4, §5.4)."""

    KIND: ClassVar[str] = "cancel_ack"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("request_id", str),
        ("phase", str),
    )

    request_id: str
    phase: str

    def __post_init__(self) -> None:
        if self.phase not in CANCEL_ACK_PHASES:
            raise UnexpectedFrameError(
                f"cancel_ack.phase must be one of {CANCEL_ACK_PHASES}, got {self.phase!r}"
            )


@dataclass(frozen=True)
class ControlAck(Message):
    """``control_ack`` -- the runtime's answer to a ``control`` frame (§4.4)."""

    KIND: ClassVar[str] = "control_ack"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("request_id", str),
        ("action", str),
        ("ok", bool),
        ("status", str),
    )

    request_id: str
    action: str
    ok: bool
    status: str


@dataclass(frozen=True)
class ConfigAck(Message):
    """``config_ack`` -- the runtime's verdict on one ``config_update`` (§4.9).

    ``tier`` is the answer that cannot be faked: ``hot_swap`` means the running
    agent will apply the change at its next turn boundary without being rebuilt,
    ``requires_respawn`` means it will not -- a caller that reports "applied" on
    top of a ``requires_respawn`` ack is reporting something this side never
    promised. ``status`` carries the human-readable detail (``queued``,
    ``stashed``, ``invalid agent_config``, ...) that ``tier`` deliberately does
    not.
    """

    KIND: ClassVar[str] = "config_ack"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("request_id", str),
        ("ok", bool),
        ("tier", str),
        ("status", str),
    )

    request_id: str
    ok: bool
    tier: str
    status: str

    def __post_init__(self) -> None:
        if self.tier not in CONFIG_TIERS:
            raise UnexpectedFrameError(
                f"config_ack.tier must be one of {CONFIG_TIERS}, got {self.tier!r}"
            )


@dataclass(frozen=True)
class Fatal(Message):
    """``fatal`` -- the runtime is about to exit non-zero (§4.4)."""

    KIND: ClassVar[str] = "fatal"
    _FIELDS: ClassVar[Tuple[Tuple[str, Any], ...]] = (
        ("reason", str),
        ("traceback_tail", str),
    )

    reason: str
    traceback_tail: str


@dataclass(frozen=True)
class Bye(Message):
    """``bye`` -- orderly completion of ``shutdown``; payload ``{}`` (§4.4)."""

    KIND: ClassVar[str] = "bye"


#: ``kind`` -> message type. Explicit rather than discovered: this table IS
#: the closed set §4.5 refuses to widen by accident, so adding a kind must be
#: a deliberate two-line change here and in the direction tables below.
MESSAGE_TYPES: Dict[str, Type[Message]] = {
    DispatchQuery.KIND: DispatchQuery,
    Cancel.KIND: Cancel,
    Control.KIND: Control,
    ConfigUpdate.KIND: ConfigUpdate,
    Shutdown.KIND: Shutdown,
    Ping.KIND: Ping,
    Hello.KIND: Hello,
    QueryAccepted.KIND: QueryAccepted,
    Reply.KIND: Reply,
    Event.KIND: Event,
    Heartbeat.KIND: Heartbeat,
    WorkerState.KIND: WorkerState,
    CancelAck.KIND: CancelAck,
    ControlAck.KIND: ControlAck,
    ConfigAck.KIND: ConfigAck,
    Fatal.KIND: Fatal,
    Bye.KIND: Bye,
}

#: Every kind this build knows, in either direction.
ALL_KINDS: Tuple[str, ...] = tuple(MESSAGE_TYPES)

#: Which kinds may appear on which direction (§4.3 / §4.4).
KINDS_BY_DIRECTION: Dict[str, Tuple[str, ...]] = {
    DIRECTION_TO_RUNTIME: (
        "dispatch_query",
        "cancel",
        "control",
        "config_update",
        "shutdown",
        "ping",
    ),
    DIRECTION_FROM_RUNTIME: (
        "hello",
        "query_accepted",
        "reply",
        "event",
        "heartbeat",
        "worker_state",
        "cancel_ack",
        "control_ack",
        "config_ack",
        "fatal",
        "bye",
    ),
}


def check_kind(kind: str, direction: str) -> None:
    """Refuse an unknown ``kind``, or a known one travelling the wrong way.

    Never ignored (§4.5): a stream we cannot interpret may be mid-frame, so
    the only safe reading of an unknown ``kind`` is "this connection is
    lost". A kind from the other direction is refused for the same reason --
    a ``reply`` arriving at a runtime means one of the two peers is confused
    about which end it is on, and guessing would hide a real desync.
    """
    _check_direction(direction)
    if kind in KINDS_BY_DIRECTION[direction]:
        return
    if kind in MESSAGE_TYPES:
        raise UnexpectedFrameError(
            f"kind {kind!r} is not part of the {direction} direction (§4.3/§4.4)"
        )
    raise UnexpectedFrameError(
        f"unknown kind {kind!r}: a stream we cannot interpret may be mid-frame (§4.5)"
    )


def _validate_envelope(body: Any) -> Dict[str, Any]:
    """Structural gate shared by encode and decode (§4.7).

    Checks types with ``type(x) is ...`` so ``True`` can never pass for ``1``
    in ``v`` or ``seq`` -- the same strictness ``CommandFence`` applies to its
    integer axes, and for the same reason: a bool that compares equal to a
    valid int is a silent identity collision, not a parse.
    """
    if not isinstance(body, dict):
        raise FrameDecodeError(
            f"frame body must be a JSON object, got {type(body).__name__} (§4.7)"
        )
    for key in REQUIRED_ENVELOPE_KEYS:
        if key not in body:
            raise FrameDecodeError(f"frame body is missing the {key!r} key (§4.7)")
    if type(body["v"]) is not int:
        raise FrameDecodeError(
            f"envelope v must be an int, got {type(body['v']).__name__} (§4.5)"
        )
    if not isinstance(body["kind"], str) or not body["kind"]:
        raise FrameDecodeError(
            f"envelope kind must be a non-empty str, got {type(body['kind']).__name__}"
        )
    if type(body["seq"]) is not int:
        raise FrameDecodeError(
            f"envelope seq must be an int, got {type(body['seq']).__name__} (§4.2)"
        )
    if body["v"] != PROTOCOL_VERSION:
        raise UnexpectedFrameError(
            f"unsupported protocol version {body['v']}: this build speaks "
            f"v{PROTOCOL_VERSION} (§4.5)"
        )
    return body


def _validate_envelope_for_encode(body: Any) -> Dict[str, Any]:
    """``_validate_envelope`` with every refusal attributed to the writer.

    The same structure is checked at both ends, but the diagnosis differs: a
    body that fails these checks on the way out was built by *this* process,
    so the fault is here and not at the peer. Converted by exception rather
    than re-checked so there is one source of truth -- a second copy of these
    rules would drift, and the copy that drifts would be the one that is
    wrong.
    """
    try:
        return _validate_envelope(body)
    except WorkerRuntimeProtocolError as exc:
        raise FrameEncodeError(str(exc)) from exc


def encode_frame(frame: Dict[str, Any]) -> bytes:
    """Serialise one envelope to ``length prefix + UTF-8 JSON`` (§4.2).

    The envelope is validated on the way out too: an over-limit or malformed
    frame must fail at the writer, not at the peer, because a peer that
    receives it is required to kill the connection (§4.7).
    """
    body = _validate_envelope_for_encode(frame)
    try:
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise FrameEncodeError(
            f"frame is not JSON-serialisable: {exc}"
        ) from exc
    _check_body_length(len(text), FrameEncodeError)
    return _LENGTH_PREFIX.pack(len(text)) + text


def decode_frame(body: bytes, *, direction: Optional[str] = None) -> Dict[str, Any]:
    """Parse one frame *body* (no length prefix) back into an envelope.

    *direction* is optional here and mandatory on :class:`FrameDecoder`: a
    bare body can be inspected without knowing where it came from, but a
    stream cannot, because ``seq`` and the kind table are per-direction.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise FrameDecodeError(
            f"frame body must be bytes, got {type(body).__name__}"
        )
    raw = bytes(body)
    _check_body_length(len(raw))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrameDecodeError(f"frame body is not valid UTF-8: {exc} (§4.7)") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FrameDecodeError(f"frame body is not JSON: {exc} (§4.7)") from exc
    frame = _validate_envelope(parsed)
    if direction is not None:
        check_kind(frame["kind"], direction)
    return frame


@dataclass(frozen=True)
class ExpectedFence:
    """The three socket-side fence axes a receiver expects on every frame.

    A local mirror of ``CommandFence``'s ``worker`` / ``instance`` / ``epoch``
    -- deliberately *not* an import of :mod:`tools.workspace.command_channel`.
    That module is filesystem-bound (atomic replace, ``fsync``, ``O_EXCL``
    creation) while this codec is stdlib-only and has to run unchanged inside
    the child process, so importing it would drag a whole filesystem contract
    across a boundary that must not have one. The three fields are copied;
    its ``session_id`` is not, because a session identifies the supervisor
    itself and says nothing about which incarnation *sent* the frame.

    Used by :class:`FrameDecoder` when a caller opts in: a frame whose axes
    disagree with this fence is dropped, counted and reported, never raised
    (§4.7 row 7).
    """

    worker: str
    instance: int
    epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.worker, str) or not self.worker:
            raise ValueError(f"worker must be a non-empty str, got {self.worker!r}")
        for name in ("instance", "epoch"):
            value = getattr(self, name)
            if type(value) is not int:
                raise ValueError(f"{name} must be an int, got {type(value).__name__}")

    def reject_reason(self, frame: Mapping[str, Any]) -> Optional[str]:
        """Why *frame* is from another incarnation, or ``None`` if it is not.

        ``type(actual) is not type(expected)`` is checked as well as ``!=`` so
        a JSON ``true`` cannot stand in for ``1`` -- the same
        bool-is-not-int rule the envelope validator and ``CommandFence``
        apply, for the same reason.
        """
        for axis in ("worker", "instance", "epoch"):
            expected = getattr(self, axis)
            actual = frame.get(axis)
            if type(actual) is not type(expected) or actual != expected:
                return (
                    f"{axis}={actual!r} does not match the expected {expected!r}: "
                    "frame is from another incarnation (§4.7)"
                )
        return None


class SeqTracker:
    """Receive-side ``seq`` guard for ONE direction of ONE connection (§4.2).

    One instance per direction: the two directions have independent
    sequences, both starting at 1, so a single shared guard would reject the
    very first interleaved frame for no reason.
    """

    def __init__(self, direction: str) -> None:
        self.direction = _check_direction(direction)
        #: Highest ``seq`` accepted so far; 0 means "nothing observed yet".
        self.last_seq = 0
        #: Number of frames accepted, useful for the desync log line.
        self.count = 0

    def observe(self, seq: int) -> int:
        """Record *seq*, refusing anything but 1 first and an increase after.

        Gaps are allowed on purpose: §4.6 lets the outbox coalesce
        (drop-oldest) frames that already carry a ``seq``, so ``5`` after
        ``3`` is legal while ``3`` after ``3`` (or ``2``) is a desync and
        kills the connection (§4.7).
        """
        if self.count == 0:
            if seq != 1:
                raise FrameDecodeError(
                    f"{self.direction}: first seq must be 1, got {seq} (§4.2)"
                )
        elif seq <= self.last_seq:
            raise FrameDecodeError(
                f"{self.direction}: seq {seq} is not strictly increasing after "
                f"{self.last_seq}; stream is out of sync (§4.7)"
            )
        self.last_seq = seq
        self.count += 1
        return seq


class FrameDecoder:
    """Incremental decoder for one direction of one connection (§4.2).

    ``feed`` accepts any chunking: a partial frame is buffered and is *not* an
    error, and one read may complete several frames. Both are why this is a
    class and not a function -- the wire is a byte stream, not a message
    stream, so the only correct unit of input is "however many bytes the
    socket happened to hand over".

    After any protocol error the decoder is closed (:attr:`broken`): the
    remaining bytes on a desynced stream cannot be trusted, and continuing to
    parse them is exactly the "mid-frame" hazard §4.7 names.

    SINGLE READER, ON PURPOSE: ``feed`` is not thread-safe and carries no
    lock. One decoder serves one direction of one connection and one thread
    reads that direction -- which is the shape §4.2 already describes, since
    the wire serialises its two directions itself. A lock here would buy
    nothing and would imply a sharing model the protocol does not have; the
    send side, where several producers genuinely do converge on one counter,
    is where the lock lives (:class:`FrameEmitter`).
    """

    def __init__(
        self,
        direction: str,
        *,
        expected_fence: Optional[ExpectedFence] = None,
        on_fence_reject: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.direction = _check_direction(direction)
        #: Unparsed bytes. After every SUCCESSFUL ``feed`` this holds fewer
        #: than ``LENGTH_PREFIX_BYTES + MAX_FRAME_BYTES`` -- the largest frame
        #: that could still be incomplete -- because a length prefix is
        #: validated before its body is awaited. It is NOT a bound on the
        #: input: ``feed`` appends whatever it is handed and chews through it
        #: in the same call, so one huge chunk is buffered only for as long as
        #: that loop runs. Nothing survives an error: the ``_broken`` latch
        #: clears the buffer and refuses every later call.
        self._buffer = bytearray()
        self._broken: Optional[str] = None
        #: The ``seq`` guard for this direction (§4.2, §4.8).
        self.seq = SeqTracker(self.direction)
        #: Opt-in incarnation check (§4.7 row 7); ``None`` accepts any fence.
        self.expected_fence = expected_fence
        self._on_fence_reject = on_fence_reject
        #: Frames dropped for carrying a foreign fence. Evidence, not control:
        #: the codec only counts them -- what a threshold means (§4.7) is the
        #: supervisor's policy, not the codec's.
        self.fence_rejects = 0

    @property
    def broken(self) -> bool:
        """True once a protocol error has closed this decoder (§4.7)."""
        return self._broken is not None

    @property
    def buffered(self) -> int:
        """Bytes held for an incomplete frame; 0 when the stream is in step."""
        return len(self._buffer)

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        """Append *data* and return every frame that is now complete.

        Atomic with respect to the frames it completes: on a protocol error
        nothing is returned. The frames already decoded earlier in this same
        call were real -- their ``seq`` values are consumed and cannot be
        un-seen -- so they are attached to the exception as
        :attr:`WorkerRuntimeProtocolError.decoded_frames`, and every buffered
        byte is discarded because a desynced stream has no safe resumption
        point (§4.7).
        """
        if self._broken is not None:
            raise FrameDecodeError(
                f"{self.direction}: decoder closed after a protocol error "
                f"({self._broken}) (§4.7)"
            )
        if not isinstance(data, (bytes, bytearray)):
            raise FrameDecodeError(
                f"feed takes bytes, got {type(data).__name__}"
            )
        self._buffer += data
        frames: List[Dict[str, Any]] = []
        try:
            while len(self._buffer) >= LENGTH_PREFIX_BYTES:
                (length,) = _LENGTH_PREFIX.unpack_from(self._buffer, 0)
                _check_body_length(length)
                end = LENGTH_PREFIX_BYTES + length
                if len(self._buffer) < end:
                    # Partial frame: buffered, NOT an error (§4.2).
                    break
                body = bytes(self._buffer[LENGTH_PREFIX_BYTES:end])
                del self._buffer[:end]
                frame = decode_frame(body, direction=self.direction)
                self.seq.observe(frame["seq"])
                if self._drop_if_foreign(frame):
                    continue
                frames.append(frame)
        except WorkerRuntimeProtocolError as exc:
            exc.decoded_frames = tuple(frames)
            self._buffer.clear()
            self._broken = str(exc)
            raise
        return frames

    def _drop_if_foreign(self, frame: Dict[str, Any]) -> bool:
        """True if *frame* belongs to another incarnation and was dropped.

        Called only after the frame parses and after its ``seq`` has been
        observed: §4.7 lists the fence rule last, and a frame that cannot be
        decoded at all is a decode error rather than a stale-generation frame
        -- the stream tells us which of the two it is before the envelope
        does. The ``seq`` is consumed either way, because it was the sender's
        number and the next frame must still advance past it.

        Dropping is not an error (§4.7 row 7): this never raises and never
        closes the connection. A generation that has been replaced can leave
        real frames in flight, and a ``heartbeat`` from a dead incarnation is
        something to discard -- not a reason to ``SIGKILL`` a healthy runtime
        mid-query. The evidence is :attr:`fence_rejects` plus whatever the
        caller's *on_fence_reject* callback logs; dropping a frame silently
        would be the one thing the command channel never does with a mismatch.
        """
        if self.expected_fence is None:
            return False
        reason = self.expected_fence.reject_reason(frame)
        if reason is None:
            return False
        self.fence_rejects += 1
        if self._on_fence_reject is not None:
            self._on_fence_reject(reason)
        return True


class FrameEmitter:
    """Send-side sequencer for ONE direction of ONE connection (§4.2, §4.4).

    One instance per direction. It stamps ``worker`` / ``instance`` / ``epoch``
    from its own fence exactly as ``CommandFence`` does, so a frame cannot
    drift from the incarnation that emitted it, and it alone owns the
    ``seq`` counter -- a counter shared with the other direction would let one
    side's traffic order the other's.
    """

    def __init__(self, direction: str, *, worker: str, instance: int, epoch: int) -> None:
        self.direction = _check_direction(direction)
        if not isinstance(worker, str) or not worker:
            raise ValueError(f"worker must be a non-empty str, got {worker!r}")
        for name, value in (("instance", instance), ("epoch", epoch)):
            if type(value) is not int:
                raise ValueError(
                    f"{name} must be an int, got {type(value).__name__}"
                )
        self.worker = worker
        self.instance = instance
        self.epoch = epoch
        #: Highest ``seq`` emitted; the next frame carries ``last_seq + 1``.
        self.last_seq = 0
        #: ``emit`` is reached from several threads (replies, heartbeats,
        #: events), and a repeated ``seq`` is a desync the peer is required to
        #: kill the connection over (§4.7), so the read-modify-write of
        #: ``last_seq`` is serialised.
        self._lock = threading.Lock()

    def emit(self, message: Message) -> Dict[str, Any]:
        """Return the complete envelope for *message* at the next ``seq``.

        Thread-safe: allocation of the number and the build of the frame
        happen under one lock, so two concurrent callers can never be handed
        the same ``seq``.
        """
        if not isinstance(message, Message):
            raise TypeError(f"emit takes a Message, got {type(message).__name__}")
        check_kind(message.KIND, self.direction)
        with self._lock:
            seq = self.last_seq + 1
            frame = message.to_frame(
                seq=seq,
                worker=self.worker,
                instance=self.instance,
                epoch=self.epoch,
            )
            # Consumed only after the frame is built, so a failed emit cannot
            # leave a hole in the sequence the peer is required to see.
            self.last_seq = seq
        return frame

    def encode(self, message: Message) -> bytes:
        """Return *message* as length-prefixed bytes at the next ``seq``."""
        return encode_frame(self.emit(message))
