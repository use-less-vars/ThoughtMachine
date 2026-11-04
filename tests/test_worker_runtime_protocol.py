"""Tests for the P3 worker-runtime wire protocol and the S0 runtime handle.

Two modules under test, both new in stage S0:

* ``tools/workspace/worker_runtime_protocol.py`` -- the §4 codec. Everything in
  it is pure: a frame goes in, bytes come out, and nothing reads a clock, a
  socket or the filesystem. That is exactly why it can be tested exhaustively
  here, one kind at a time, with no worker and no second process -- and why it
  MUST be, because S2 will put a real ``AF_UNIX`` socketpair underneath it and
  a codec bug found there looks like a hang rather than a failed assertion.
* ``tools/workspace/worker_runtime.py`` -- the S0 seam. Every method is a
  direct call to the ``WorkerThread`` handed in, so the assertions are about
  *delegation* and *mode resolution*, not about behaviour: §11.1 promises S0 is
  observably identical to calling the thread directly, and the only way to keep
  that promise honest is to pin that the handle adds nothing (no thread, no
  queue, no cached flag) and never shapes a return value.

What the assertions are really protecting
----------------------------------------
§4.7 splits protocol violations into "the stream is malformed" and "the frame
parsed but says something we cannot accept", and the receiver reacts the same
way to both (close -> SIGKILL -> ``lost{protocol_error}``). The tests keep the
two classes apart -- :class:`FrameDecodeError` versus
:class:`UnexpectedFrameError` -- because the distinction is the only thing that
makes the log line readable. Alongside that, three rules are pinned
individually because each is easy to "helpfully" break later:

* a frame at exactly ``MAX_FRAME_BYTES`` is legal and one byte more is not, so
  the boundary is tested from both sides with a body of exactly that size
  (built by padding, not by hoping);
* ``seq`` is **per direction**, so two senders legitimately both start at 1 and
  interleave without tripping each other -- a single shared counter would
  reject the second direction's first frame;
* an unknown *extra payload key* is ignored (§4.5, additive) while an unknown
  *kind* is fatal (§4.5) -- the two rules point opposite ways on purpose and
  are asserted next to each other so neither is "harmonised" by accident.

A gap in ``seq`` is legal (§4.6 coalescing drops already-sequenced frames); a
repeat or a step backwards is a desync. All three cases are covered.
"""

from __future__ import annotations

import json
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tools.workspace.worker_runtime import (
    MODE_PROCESS,
    MODE_THREAD,
    WORKER_RUNTIME_MODE_ENV,
    InvalidRuntimeModeError,
    WorkerRuntimeHandle,
    resolve_runtime_mode,
)
from tools.workspace.worker_runtime_protocol import (
    CANCEL_ACK_PHASES,
    CONTROL_ACTIONS,
    DIRECTION_FROM_RUNTIME,
    DIRECTION_TO_RUNTIME,
    DIRECTIONS,
    KINDS_BY_DIRECTION,
    LENGTH_PREFIX_BYTES,
    MAX_FRAME_BYTES,
    MESSAGE_TYPES,
    PROTOCOL_VERSION,
    REQUIRED_ENVELOPE_KEYS,
    Bye,
    Cancel,
    CONFIG_TIER_HOT_SWAP,
    CancelAck,
    ConfigAck,
    ConfigUpdate,
    Control,
    ControlAck,
    DispatchQuery,
    Event,
    ExpectedFence,
    Fatal,
    FrameDecodeError,
    FrameDecoder,
    FrameEmitter,
    FrameEncodeError,
    Heartbeat,
    Hello,
    Message,
    Ping,
    QueryAccepted,
    Reply,
    Shutdown,
    UnexpectedFrameError,
    WorkerRuntimeProtocolError,
    WorkerState,
    check_kind,
    decode_frame,
    encode_frame,
)
from tools.workspace.worker_thread import WorkerThread

#: The fence every frame carries (§4.2). Overridden per test where the point
#: IS the fence, and fixed here otherwise so failure output stays readable.
WORKER = "w8"
INSTANCE = 2
EPOCH = 5
FENCE = {"worker": WORKER, "instance": INSTANCE, "epoch": EPOCH}

#: One representative value of every kind (§4.3 / §4.4). Parametrising over
#: this list -- rather than over the classes -- means a kind added to the table
#: without a sample fails ``test_every_known_kind_has_a_sample`` instead of
#: quietly going untested.
SAMPLES = (
    DispatchQuery(query_id="q-1", query="what is 2 + 2?", timeout_seconds=120.0),
    Cancel(request_id="r-1", reason="ceiling", deadline_ms=500),
    Control(request_id="r-2", action="stop"),
    ConfigUpdate(request_id="r-3", config={"model": "m2"}),
    Shutdown(grace_ms=2000),
    Ping(nonce=7),
    Hello(
        pid=4242,
        started_at="2025-01-01T00:00:00+00:00",
        runtime_version="1.0",
        epoch_seen=3,
    ),
    QueryAccepted(query_id="q-1"),
    Reply(query_id="q-1", ok=True, envelope={"text": "4"}, elapsed_seconds=0.25),
    Event(event_type="thinking", data={"step": 1, "note": "planning"}),
    Heartbeat(
        at="2025-01-01T00:00:00+00:00",
        status="working",
        current_context_tokens=10,
        max_context_tokens=100,
    ),
    WorkerState(status="idle", current_task=None, error=None, paused_manually=False),
    CancelAck(request_id="r-1", phase="completed"),
    ControlAck(request_id="r-2", action="stop", ok=True, status="detached"),
    ConfigAck(
        request_id="r-3",
        ok=True,
        tier=CONFIG_TIER_HOT_SWAP,
        status="queued for the next turn boundary",
    ),
    Fatal(reason="boom", traceback_tail="Traceback (most recent call last): ..."),
    Bye(),
)

SAMPLES_BY_KIND = {message.KIND: message for message in SAMPLES}

#: A minimal well-formed envelope, mutated key by key by the malformed-frame
#: tests. Copied before every mutation (``dict(...)``) so the tests are order
#: independent.
BASE_ENVELOPE = {
    "v": PROTOCOL_VERSION,
    "kind": "ping",
    "seq": 1,
    "worker": WORKER,
    "instance": INSTANCE,
    "epoch": EPOCH,
    "payload": {"nonce": 1},
}


def _direction_for(kind: str) -> str:
    """The single direction *kind* is legal on (§4.3 / §4.4)."""
    for direction, kinds in KINDS_BY_DIRECTION.items():
        if kind in kinds:
            return direction
    raise AssertionError(f"{kind!r} is in no direction table")


def _other_direction(direction: str) -> str:
    return (
        DIRECTION_FROM_RUNTIME
        if direction == DIRECTION_TO_RUNTIME
        else DIRECTION_TO_RUNTIME
    )


def _emitter(direction: str) -> FrameEmitter:
    return FrameEmitter(direction, worker=WORKER, instance=INSTANCE, epoch=EPOCH)


def _body(frame: dict) -> bytes:
    """The JSON body of *frame*, byte-for-byte as ``encode_frame`` writes it."""
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _framed(body: bytes) -> bytes:
    """*body* behind a 4-byte big-endian length prefix (§4.2)."""
    return struct.pack(">I", len(body)) + body


def _reply_body_of_size(size: int):
    """A ``reply`` frame whose JSON body is *exactly* *size* bytes.

    Built by measuring the unpadded body and padding the difference, so the
    ``MAX_FRAME_BYTES`` boundary is tested at the boundary instead of wherever
    a hand-written string happened to land.
    """
    frame = Reply(
        query_id="q-1", ok=True, envelope={"pad": ""}, elapsed_seconds=0.5
    ).to_frame(seq=1, **FENCE)
    deficit = size - len(_body(frame))
    assert deficit >= 0, f"{size} is below the bare reply size"
    frame["payload"]["envelope"]["pad"] = "x" * deficit
    body = _body(frame)
    assert len(body) == size
    return frame, body


# --------------------------------------------------------------------------
# The tables themselves: closed set, two directions, no kind in both
# --------------------------------------------------------------------------


def test_every_known_kind_has_a_sample():
    assert {message.KIND for message in SAMPLES} == set(MESSAGE_TYPES)
    assert len(SAMPLES) == len(MESSAGE_TYPES)


def test_direction_tables_partition_every_kind_exactly_once():
    listed = [kind for kinds in KINDS_BY_DIRECTION.values() for kind in kinds]
    assert set(KINDS_BY_DIRECTION) == set(DIRECTIONS)
    assert sorted(listed) == sorted(MESSAGE_TYPES)
    assert len(listed) == len(set(listed)), "a kind appears in both directions"


def test_the_sample_table_matches_the_declared_kinds():
    for message in SAMPLES:
        assert MESSAGE_TYPES[message.KIND] is type(message)
        assert message.to_payload().keys() == {
            name for name, _ in type(message)._FIELDS
        }


# --------------------------------------------------------------------------
# Round-trip: encode -> decode -> the same typed message
# --------------------------------------------------------------------------


@pytest.mark.parametrize("message", SAMPLES, ids=[m.KIND for m in SAMPLES])
def test_every_kind_survives_a_round_trip(message: Message):
    direction = _direction_for(message.KIND)
    frames = FrameDecoder(direction).feed(_emitter(direction).encode(message))
    assert len(frames) == 1
    frame = frames[0]
    assert frame["kind"] == message.KIND
    assert frame["v"] == PROTOCOL_VERSION
    assert frame["seq"] == 1
    assert (frame["worker"], frame["instance"], frame["epoch"]) == (
        WORKER,
        INSTANCE,
        EPOCH,
    )
    assert frame["payload"] == message.to_payload()
    assert type(message).from_frame(frame) == message


def test_a_bye_carries_an_empty_payload():
    frame = _emitter(DIRECTION_FROM_RUNTIME).emit(Bye())
    assert frame["payload"] == {}
    assert Bye.from_frame(frame) == Bye()


@pytest.mark.parametrize(
    "worker,instance,epoch",
    [
        ("w1", 1, 1),
        ("w\u00f6rker-\u03a9", 0, 0),
        ("w" * 64, 2**31, 7),
    ],
)
def test_the_fence_fields_survive_the_wire(worker, instance, epoch):
    emitter = FrameEmitter(
        DIRECTION_TO_RUNTIME, worker=worker, instance=instance, epoch=epoch
    )
    frame = FrameDecoder(DIRECTION_TO_RUNTIME).feed(emitter.encode(Ping(nonce=3)))[0]
    assert (frame["worker"], frame["instance"], frame["epoch"]) == (
        worker,
        instance,
        epoch,
    )


def test_non_ascii_payload_survives_and_the_prefix_counts_bytes():
    message = DispatchQuery(
        query_id="q-\u00fc", query="h\u00e9llo \u2014 \u4e16\u754c \U0001f40d", timeout_seconds=1.5
    )
    direction = DIRECTION_TO_RUNTIME
    encoded = _emitter(direction).encode(message)
    (length,) = struct.unpack(">I", encoded[:LENGTH_PREFIX_BYTES])
    assert length == len(encoded) - LENGTH_PREFIX_BYTES
    # Multi-byte characters: the body is longer in bytes than in characters,
    # so a length counted in characters would have been rejected here.
    assert length > len(encoded[LENGTH_PREFIX_BYTES:].decode("utf-8"))
    frames = FrameDecoder(direction).feed(encoded)
    assert DispatchQuery.from_frame(frames[0]) == message


# --------------------------------------------------------------------------
# Framing (§4.2): the length prefix and the size ceiling
# --------------------------------------------------------------------------


def test_encode_is_a_length_prefixed_body():
    frame = Ping(nonce=9).to_frame(seq=1, **FENCE)
    encoded = encode_frame(frame)
    (length,) = struct.unpack(">I", encoded[:LENGTH_PREFIX_BYTES])
    assert length == len(encoded) - LENGTH_PREFIX_BYTES
    assert encoded[LENGTH_PREFIX_BYTES:] == _body(frame)


def test_exactly_the_maximum_body_is_accepted():
    frame, body = _reply_body_of_size(MAX_FRAME_BYTES)
    encoded = encode_frame(frame)
    (length,) = struct.unpack(">I", encoded[:LENGTH_PREFIX_BYTES])
    assert length == MAX_FRAME_BYTES
    assert decode_frame(body, direction=DIRECTION_FROM_RUNTIME) == frame
    frames = FrameDecoder(DIRECTION_FROM_RUNTIME).feed(encoded)
    assert [f["kind"] for f in frames] == ["reply"]


def test_one_byte_over_the_maximum_is_refused():
    frame, body = _reply_body_of_size(MAX_FRAME_BYTES + 1)
    with pytest.raises(FrameDecodeError, match="exceeds"):
        decode_frame(body)
    # The writer refuses too, and as a WRITER error: a peer that received it
    # would have to kill the connection, so the frame must never leave this
    # process -- but the fault would be ours, not the peer's.
    with pytest.raises(FrameEncodeError, match="exceeds"):
        encode_frame(frame)
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    with pytest.raises(FrameDecodeError, match="exceeds"):
        decoder.feed(_framed(body))
    assert decoder.broken is True


def test_an_over_limit_prefix_is_refused_before_any_body_arrives():
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    with pytest.raises(FrameDecodeError, match="exceeds"):
        decoder.feed(struct.pack(">I", MAX_FRAME_BYTES + 1))
    assert decoder.broken is True


def test_a_legal_prefix_with_an_oversized_body_retains_nothing():
    # A prefix at the ceiling with far more data than it announces behind it.
    # The stream is already dead at that point, and the megabytes that arrived
    # with it must not sit in the buffer waiting for a caller that has no way
    # to reach them again.
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    junk = struct.pack(">I", MAX_FRAME_BYTES) + b"\x00" * (5 * 1024 * 1024)
    with pytest.raises(FrameDecodeError):
        decoder.feed(junk)
    assert decoder.buffered == 0
    assert decoder.broken is True


def test_a_zero_length_frame_is_refused():
    with pytest.raises(FrameDecodeError, match="zero-length"):
        decode_frame(b"")
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    with pytest.raises(FrameDecodeError, match="zero-length"):
        decoder.feed(struct.pack(">I", 0))
    assert decoder.broken is True


@pytest.mark.parametrize(
    "body,match",
    [
        (b"\xff\xfe{}", "not valid UTF-8"),
        (b"not json at all", "not JSON"),
        (b"[]", "must be a JSON object"),
        (b'"hello"', "must be a JSON object"),
        (b"null", "must be a JSON object"),
        (b"42", "must be a JSON object"),
    ],
)
def test_a_body_that_is_not_a_json_object_is_malformed(body, match):
    with pytest.raises(FrameDecodeError, match=match):
        decode_frame(body)
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    with pytest.raises(FrameDecodeError, match=match):
        decoder.feed(_framed(body))
    assert decoder.broken is True


# --------------------------------------------------------------------------
# Envelope validation (§4.7)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", list(REQUIRED_ENVELOPE_KEYS))
def test_a_missing_envelope_key_is_refused(key):
    body = {k: v for k, v in BASE_ENVELOPE.items() if k != key}
    with pytest.raises(FrameDecodeError, match=key):
        decode_frame(_body(body))


@pytest.mark.parametrize(
    "field,value",
    [
        ("v", "1"),
        ("v", 1.0),
        ("v", None),
        ("v", []),
        ("kind", 1),
        ("kind", None),
        ("kind", ""),
        ("kind", []),
        ("seq", "1"),
        ("seq", 1.0),
        ("seq", None),
    ],
)
def test_a_mistyped_envelope_key_is_refused(field, value):
    body = dict(BASE_ENVELOPE)
    body[field] = value
    with pytest.raises(FrameDecodeError):
        decode_frame(_body(body))


@pytest.mark.parametrize("field", ["v", "seq"])
def test_true_is_not_accepted_where_an_integer_is_required(field):
    # ``True == 1``, so an isinstance check would let a bool through and give
    # the peer a version or a sequence number it never wrote. The module uses
    # ``type(x) is int`` for exactly this reason.
    body = dict(BASE_ENVELOPE)
    body[field] = True
    with pytest.raises(FrameDecodeError):
        decode_frame(_body(body))


def test_an_unsupported_version_is_an_unexpected_value_not_a_parse_error():
    body = dict(BASE_ENVELOPE)
    body["v"] = PROTOCOL_VERSION + 1
    with pytest.raises(UnexpectedFrameError, match="unsupported protocol version"):
        decode_frame(_body(body))


# --------------------------------------------------------------------------
# The write side has its own error class (§4.7)
# --------------------------------------------------------------------------


def test_the_encode_error_is_a_sibling_of_the_decode_errors():
    assert issubclass(FrameEncodeError, WorkerRuntimeProtocolError)
    # Not a decode error: "I could not read this" would blame the peer for our
    # own envelope, and the two send the reader to opposite ends of the socket.
    assert not issubclass(FrameEncodeError, FrameDecodeError)
    assert not issubclass(FrameEncodeError, UnexpectedFrameError)
    # The shared base still catches everything, so a caller that does not care
    # which end was at fault keeps working.
    with pytest.raises(WorkerRuntimeProtocolError):
        encode_frame({"kind": "ping"})


@pytest.mark.parametrize("mutation", ["missing-key", "unsupported-version"])
def test_a_bad_envelope_is_refused_on_the_write_side_as_an_encode_error(mutation):
    frame = dict(BASE_ENVELOPE)
    if mutation == "missing-key":
        del frame["kind"]
        match = "'kind'"
    else:
        frame["v"] = PROTOCOL_VERSION + 1
        match = "unsupported protocol version"
    with pytest.raises(FrameEncodeError, match=match) as excinfo:
        encode_frame(frame)
    assert not isinstance(excinfo.value, FrameDecodeError)


def test_a_payload_that_is_not_json_is_refused_as_an_encode_error():
    frame = Event(event_type="x", data={"bad": object()}).to_frame(seq=1, **FENCE)
    with pytest.raises(FrameEncodeError, match="not JSON-serialisable"):
        encode_frame(frame)


# --------------------------------------------------------------------------
# Unknown kinds are fatal; unknown payload keys are not (§4.5, two opposite
# rules that live one line apart in the module)
# --------------------------------------------------------------------------


def test_an_unknown_kind_is_never_ignored():
    body = dict(BASE_ENVELOPE)
    body["kind"] = "teleport"
    # Without a direction the body still parses -- it is the direction check
    # that knows which kinds exist.
    assert decode_frame(_body(body))["kind"] == "teleport"
    with pytest.raises(UnexpectedFrameError, match="unknown kind"):
        decode_frame(_body(body), direction=DIRECTION_TO_RUNTIME)
    decoder = FrameDecoder(DIRECTION_TO_RUNTIME)
    with pytest.raises(UnexpectedFrameError, match="unknown kind"):
        decoder.feed(_framed(_body(body)))
    assert decoder.broken is True


@pytest.mark.parametrize("message", SAMPLES, ids=[m.KIND for m in SAMPLES])
def test_each_kind_is_refused_on_the_other_direction(message: Message):
    other = _other_direction(_direction_for(message.KIND))
    with pytest.raises(UnexpectedFrameError, match="not part of"):
        check_kind(message.KIND, other)
    with pytest.raises(UnexpectedFrameError, match="not part of"):
        decode_frame(_body(message.to_frame(seq=1, **FENCE)), direction=other)


def test_extra_payload_keys_are_ignored_not_refused():
    message = SAMPLES_BY_KIND["reply"]
    frame = message.to_frame(seq=1, **FENCE)
    frame["payload"]["future_field"] = {"anything": [1, 2, None]}
    decoded = decode_frame(_body(frame), direction=DIRECTION_FROM_RUNTIME)
    # The key survives the codec (it is real JSON) ...
    assert decoded["payload"]["future_field"] == {"anything": [1, 2, None]}
    # ... and the typed reader ignores it, which is what makes additive keys
    # safe without bumping ``v``.
    assert Reply.from_frame(decoded) == message


def test_a_missing_declared_payload_field_is_refused():
    frame = SAMPLES_BY_KIND["reply"].to_frame(seq=1, **FENCE)
    del frame["payload"]["elapsed_seconds"]
    with pytest.raises(FrameDecodeError, match="elapsed_seconds"):
        Reply.from_frame(frame)


def test_an_integral_float_field_arrives_as_a_float():
    frame = SAMPLES_BY_KIND["reply"].to_frame(seq=1, **FENCE)
    frame["payload"]["elapsed_seconds"] = 3
    parsed = Reply.from_frame(frame)
    assert parsed.elapsed_seconds == 3.0
    assert type(parsed.elapsed_seconds) is float


def test_a_string_where_a_float_is_declared_is_refused():
    frame = SAMPLES_BY_KIND["reply"].to_frame(seq=1, **FENCE)
    frame["payload"]["elapsed_seconds"] = "0.25"
    with pytest.raises(UnexpectedFrameError, match="elapsed_seconds"):
        Reply.from_frame(frame)


@pytest.mark.parametrize("action", ["explode", "", "STOP", "stop "])
def test_a_control_action_outside_the_closed_set_is_refused(action):
    assert action not in CONTROL_ACTIONS
    with pytest.raises(UnexpectedFrameError, match="control.action"):
        Control(request_id="r-1", action=action)
    frame = SAMPLES_BY_KIND["control"].to_frame(seq=1, **FENCE)
    frame["payload"]["action"] = action
    with pytest.raises(UnexpectedFrameError, match="control.action"):
        Control.from_frame(frame)


@pytest.mark.parametrize("phase", ["cancelled", "", "Accepted"])
def test_a_cancel_ack_phase_outside_the_closed_set_is_refused(phase):
    assert phase not in CANCEL_ACK_PHASES
    with pytest.raises(UnexpectedFrameError, match="cancel_ack.phase"):
        CancelAck(request_id="r-1", phase=phase)
    frame = SAMPLES_BY_KIND["cancel_ack"].to_frame(seq=1, **FENCE)
    frame["payload"]["phase"] = phase
    with pytest.raises(UnexpectedFrameError, match="cancel_ack.phase"):
        CancelAck.from_frame(frame)


# --------------------------------------------------------------------------
# ``seq`` is per-direction, starts at 1, and may gap but never repeat (§4.2,
# §4.6, §4.8)
# --------------------------------------------------------------------------


def test_both_directions_start_at_one():
    assert _emitter(DIRECTION_TO_RUNTIME).emit(Ping(nonce=1))["seq"] == 1
    assert (
        _emitter(DIRECTION_FROM_RUNTIME).emit(QueryAccepted(query_id="q-1"))["seq"] == 1
    )


def test_interleaved_directions_do_not_share_one_sequence():
    to_runtime = _emitter(DIRECTION_TO_RUNTIME)
    from_runtime = _emitter(DIRECTION_FROM_RUNTIME)
    runtime_side = FrameDecoder(DIRECTION_TO_RUNTIME)
    supervisor_side = FrameDecoder(DIRECTION_FROM_RUNTIME)
    seen_to = []
    seen_from = []
    for index in range(3):
        seen_to.extend(
            runtime_side.feed(to_runtime.encode(Ping(nonce=index)))
        )
        seen_from.extend(
            supervisor_side.feed(from_runtime.encode(QueryAccepted(query_id=f"q-{index}")))
        )
    assert [f["seq"] for f in seen_to] == [1, 2, 3]
    assert [f["seq"] for f in seen_from] == [1, 2, 3]
    assert runtime_side.seq.count == 3
    assert supervisor_side.seq.count == 3


def test_a_gap_in_seq_is_legal_because_the_outbox_coalesces():
    decoder = FrameDecoder(DIRECTION_TO_RUNTIME)
    first = decoder.feed(
        _framed(_body(Ping(nonce=1).to_frame(seq=1, **FENCE)))
    )
    second = decoder.feed(
        _framed(_body(Ping(nonce=2).to_frame(seq=5, **FENCE)))
    )
    assert [f["seq"] for f in first + second] == [1, 5]
    assert decoder.seq.last_seq == 5
    assert decoder.broken is False


@pytest.mark.parametrize("seqs", [(1, 1), (2, 1), (1, 3, 2), (1, 2, 2)])
def test_a_repeat_or_a_step_backwards_closes_the_decoder(seqs):
    decoder = FrameDecoder(DIRECTION_TO_RUNTIME)
    with pytest.raises(FrameDecodeError):
        for index, seq in enumerate(seqs):
            decoder.feed(
                _framed(_body(Ping(nonce=index).to_frame(seq=seq, **FENCE)))
            )
    assert decoder.broken is True


def test_the_first_seq_must_be_one():
    decoder = FrameDecoder(DIRECTION_TO_RUNTIME)
    with pytest.raises(FrameDecodeError, match="first seq must be 1"):
        decoder.feed(_framed(_body(Ping(nonce=0).to_frame(seq=2, **FENCE))))


def test_a_closed_decoder_refuses_further_input():
    decoder = FrameDecoder(DIRECTION_TO_RUNTIME)
    with pytest.raises(FrameDecodeError):
        decoder.feed(struct.pack(">I", 0))
    with pytest.raises(FrameDecodeError, match="decoder closed"):
        decoder.feed(_framed(_body(BASE_ENVELOPE)))


# --------------------------------------------------------------------------
# Incremental decoding: the wire is a byte stream, not a message stream (§4.2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 5, 7, 64, 4096])
def test_any_chunking_yields_the_same_frames(chunk):
    emitter = _emitter(DIRECTION_FROM_RUNTIME)
    stream = b"".join(
        [
            emitter.encode(QueryAccepted(query_id="q-1")),
            emitter.encode(
                WorkerState(
                    status="idle", current_task=None, error=None, paused_manually=False
                )
            ),
        ]
    )
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    frames = []
    for start in range(0, len(stream), chunk):
        frames.extend(decoder.feed(stream[start : start + chunk]))
    assert [f["kind"] for f in frames] == ["query_accepted", "worker_state"]
    assert [f["seq"] for f in frames] == [1, 2]
    assert decoder.buffered == 0
    assert decoder.broken is False


def test_frames_decoded_before_a_failure_are_attached_to_the_error():
    # One read holding two frames: a good one and then pure garbage. The good
    # frame was really decoded -- its seq is consumed -- so it must not be
    # vaporised just because the same call raises.
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    good = _emitter(DIRECTION_FROM_RUNTIME).encode(QueryAccepted(query_id="q-1"))
    with pytest.raises(FrameDecodeError) as excinfo:
        decoder.feed(good + _framed(b"not json at all"))
    assert [f["kind"] for f in excinfo.value.decoded_frames] == ["query_accepted"]
    assert [f["seq"] for f in excinfo.value.decoded_frames] == [1]
    assert decoder.seq.count == 1
    assert decoder.buffered == 0
    assert decoder.broken is True


def test_an_error_raised_outside_the_decoder_carries_no_frames():
    with pytest.raises(FrameDecodeError) as excinfo:
        decode_frame(b"not json at all")
    assert excinfo.value.decoded_frames == ()


def test_a_partial_frame_is_buffered_rather_than_reported():
    encoded = _emitter(DIRECTION_TO_RUNTIME).encode(Ping(nonce=1))
    decoder = FrameDecoder(DIRECTION_TO_RUNTIME)
    # Split inside the length prefix, then inside the body.
    assert decoder.feed(encoded[:2]) == []
    assert decoder.buffered == 2
    assert decoder.feed(encoded[2:6]) == []
    assert decoder.buffered == 6
    frames = decoder.feed(encoded[6:])
    assert [f["kind"] for f in frames] == ["ping"]
    assert decoder.buffered == 0


# --------------------------------------------------------------------------
# FrameEmitter: the send side owns ``seq`` and the fence
# --------------------------------------------------------------------------


def test_an_emitter_refuses_a_kind_from_the_other_direction():
    emitter = _emitter(DIRECTION_TO_RUNTIME)
    with pytest.raises(UnexpectedFrameError, match="not part of"):
        emitter.emit(
            Hello(pid=1, started_at="now", runtime_version="1.0", epoch_seen=1)
        )
    # A refused emit must not burn a ``seq``: the peer is entitled to a gap-free
    # sequence, and a hole here would look exactly like §4.6 coalescing.
    assert emitter.last_seq == 0
    assert emitter.emit(Ping(nonce=1))["seq"] == 1


def test_an_emitter_rejects_a_bad_direction_fence_or_argument():
    with pytest.raises(ValueError):
        FrameEmitter("sideways", worker="w", instance=1, epoch=1)
    with pytest.raises(ValueError):
        FrameEmitter(DIRECTION_TO_RUNTIME, worker="", instance=1, epoch=1)
    with pytest.raises(ValueError):
        FrameEmitter(DIRECTION_TO_RUNTIME, worker="w", instance=True, epoch=1)
    with pytest.raises(ValueError):
        FrameEmitter(DIRECTION_TO_RUNTIME, worker="w", instance=1, epoch=True)
    with pytest.raises(TypeError):
        _emitter(DIRECTION_TO_RUNTIME).emit({"kind": "ping", "seq": 1})


def test_concurrent_emits_never_reuse_a_seq():
    emitter = _emitter(DIRECTION_TO_RUNTIME)
    emitted = []
    start_together = threading.Barrier(8)

    def emit_repeatedly(index):
        start_together.wait()
        for _ in range(25):
            emitted.append(emitter.emit(Ping(nonce=index))["seq"])

    threads = [threading.Thread(target=emit_repeatedly, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # A repeated seq is a desync the peer is REQUIRED to kill the connection
    # over (§4.7), so a lost update here would be a self-inflicted protocol
    # violation: every number 1..200 must be handed out exactly once.
    assert sorted(emitted) == list(range(1, 8 * 25 + 1))


def test_a_decoder_rejects_a_bad_direction():
    with pytest.raises(ValueError):
        FrameDecoder("sideways")
    with pytest.raises(ValueError):
        decode_frame(_body(BASE_ENVELOPE), direction="sideways")


# --------------------------------------------------------------------------
# The expected fence: a foreign frame is DROPPED, never fatal (§4.7 row 7)
# --------------------------------------------------------------------------


def _fenced_decoder(rejections=None):
    """A decoder that expects :data:`FENCE` and records what it rejects."""
    return FrameDecoder(
        DIRECTION_FROM_RUNTIME,
        expected_fence=ExpectedFence(worker=WORKER, instance=INSTANCE, epoch=EPOCH),
        on_fence_reject=(rejections.append if rejections is not None else None),
    )


def test_a_frame_that_matches_the_expected_fence_is_delivered():
    decoder = _fenced_decoder()
    encoded = _emitter(DIRECTION_FROM_RUNTIME).encode(QueryAccepted(query_id="q-1"))
    assert [f["kind"] for f in decoder.feed(encoded)] == ["query_accepted"]
    assert decoder.fence_rejects == 0


@pytest.mark.parametrize("axis", ["worker", "instance", "epoch"])
def test_a_frame_from_another_incarnation_is_dropped_and_counted(axis):
    rejections = []
    decoder = _fenced_decoder(rejections)
    foreign = dict(FENCE)
    foreign[axis] = WORKER + "-old" if axis == "worker" else FENCE[axis] + 1
    emitter = FrameEmitter(DIRECTION_FROM_RUNTIME, **foreign)
    # Dropped, not raised: a replaced generation's trailing frames are garbage
    # to discard, not a reason to SIGKILL a healthy runtime (§4.7 row 7).
    assert decoder.feed(emitter.encode(QueryAccepted(query_id="q-1"))) == []
    assert decoder.fence_rejects == 1
    assert decoder.broken is False
    # Not a silent drop either: the mismatch always leaves evidence behind.
    assert len(rejections) == 1
    assert axis in rejections[0]
    # The connection is untouched, and the foreign frame's seq stays consumed,
    # so the next legitimate frame (seq 2) still decodes.
    good = QueryAccepted(query_id="q-2").to_frame(seq=2, **FENCE)
    assert [f["seq"] for f in decoder.feed(_framed(_body(good)))] == [2]


def test_without_an_expected_fence_nothing_is_dropped():
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    foreign = FrameEmitter(
        DIRECTION_FROM_RUNTIME, worker="w-other", instance=9, epoch=9
    )
    frames = decoder.feed(foreign.encode(QueryAccepted(query_id="q-1")))
    assert [f["kind"] for f in frames] == ["query_accepted"]
    assert decoder.fence_rejects == 0


def test_a_json_true_cannot_stand_in_for_the_expected_instance():
    # ``True == 1``, so an equality-only comparison would accept a frame from
    # incarnation ``true`` as incarnation 1. The type is checked as well.
    fence = ExpectedFence(worker=WORKER, instance=1, epoch=EPOCH)
    frame = dict(BASE_ENVELOPE)
    frame["instance"] = True
    assert fence.reject_reason(frame) is not None
    frame["instance"] = 1
    assert fence.reject_reason(frame) is None


@pytest.mark.parametrize(
    "worker,instance,epoch",
    [("", 1, 1), ("w", True, 1), ("w", 1, True), ("w", 1.0, 1)],
)
def test_an_expected_fence_refuses_a_malformed_axis(worker, instance, epoch):
    with pytest.raises(ValueError):
        ExpectedFence(worker=worker, instance=instance, epoch=epoch)


# --------------------------------------------------------------------------
# Mode resolution (§10, D11): absent -> thread, anything else fails closed
# --------------------------------------------------------------------------


def test_an_absent_flag_means_thread():
    assert resolve_runtime_mode({}) == MODE_THREAD


@pytest.mark.parametrize("value", [MODE_THREAD, MODE_PROCESS])
def test_the_two_known_modes_resolve(value):
    assert resolve_runtime_mode({WORKER_RUNTIME_MODE_ENV: value}) == value


@pytest.mark.parametrize(
    "value",
    ["", "Thread", "THREAD", " thread", "thread ", "threads", "processes", "0", "none"],
)
def test_anything_else_fails_closed(value):
    # Not trimmed, not case-folded, and an explicitly empty value is a value:
    # "I did not understand the config" must never read as "run in-process".
    with pytest.raises(InvalidRuntimeModeError):
        resolve_runtime_mode({WORKER_RUNTIME_MODE_ENV: value})


def test_the_default_reads_the_process_environment(monkeypatch):
    monkeypatch.delenv(WORKER_RUNTIME_MODE_ENV, raising=False)
    assert resolve_runtime_mode() == MODE_THREAD
    monkeypatch.setenv(WORKER_RUNTIME_MODE_ENV, MODE_PROCESS)
    assert resolve_runtime_mode() == MODE_PROCESS


def test_the_flag_is_read_every_time_and_not_memoised():
    assert resolve_runtime_mode({}) == MODE_THREAD
    assert resolve_runtime_mode({WORKER_RUNTIME_MODE_ENV: MODE_PROCESS}) == MODE_PROCESS
    assert resolve_runtime_mode({}) == MODE_THREAD


# --------------------------------------------------------------------------
# WorkerRuntimeHandle: the S0 seam adds nothing (§10, §11.1)
# --------------------------------------------------------------------------


class FakeWorker:
    """The smallest stand-in for ``WorkerThread`` the handle can observe.

    No ``run``, no ``start``: if the handle ever tried to drive the thread
    itself (§3.3, D7 say it must not) the test would fail with an
    ``AttributeError`` rather than pass silently.
    """

    def __init__(self) -> None:
        self.status = "working"
        self.current_task = "task-1"
        self.error = None
        self._manual_only_pause = 1
        self.calls = []

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        self.calls.append(("send_query", query, timeout))
        return f"reply:{query}"

    def detach(self) -> None:
        self.calls.append(("detach",))


def test_spawn_does_not_drive_the_worker():
    worker = FakeWorker()
    handle = WorkerRuntimeHandle.spawn(worker, environ={})
    assert handle.mode == MODE_THREAD
    # Behaviour, not internals: the fake has no ``run``/``start``, so a handle
    # that tried to drive the thread itself (§3.3, D7) would have crashed.
    assert not hasattr(worker, "run") and not hasattr(worker, "start")
    assert worker.calls == []
    # ... and the seam is live rather than merely inert.
    assert handle.dispatch_query("hi") == "reply:hi"
    assert worker.calls == [("send_query", "hi", 120.0)]


def test_dispatch_query_delegates_verbatim():
    worker = FakeWorker()
    handle = WorkerRuntimeHandle.spawn(worker, environ={})
    assert handle.dispatch_query("hi") == "reply:hi"
    assert worker.calls == [("send_query", "hi", 120.0)]
    assert handle.dispatch_query("hey", timeout=5) == "reply:hey"
    assert worker.calls[-1] == ("send_query", "hey", 5)


def test_cancel_accepted_when_busy():
    # S3 (§5.1, §5.4): thread mode has no ``cancel`` to call -- the turn token
    # IS the signal -- and a worker that is mid-turn accepts the request.
    worker = FakeWorker()
    worker.status = "busy"
    worker._turn_cancel = threading.Event()
    assert WorkerRuntimeHandle.spawn(worker, environ={}).cancel() == "accepted"
    assert worker._turn_cancel.is_set() is True
    # Not a detach: parking the worker is ``stop``'s job, not ``cancel``'s.
    assert worker.calls == []


def test_cancel_refused_when_the_worker_is_terminal():
    # §5.4: refusal is a verdict about the worker ("nothing is running"), given
    # for the same reason the child-side handler gives it -- both modes agree.
    # S3b widened the rule from "must be busy": acceptance now depends on the
    # worker NOT being in CANCEL_TERMINAL_STATUSES, so the refusal case has to
    # be a terminal status (a "working" worker is accepted today).
    worker = FakeWorker()
    worker.status = "killed"
    worker._turn_cancel = threading.Event()
    assert WorkerRuntimeHandle.spawn(worker, environ={}).cancel() == "refused"
    assert worker._turn_cancel.is_set() is False
    assert worker.calls == []


def test_state_mirrors_the_thread_in_the_worker_state_shape():
    worker = FakeWorker()
    handle = WorkerRuntimeHandle.spawn(worker, environ={})
    assert handle.state() == WorkerState(
        status="working",
        current_task="task-1",
        error=None,
        paused_manually=True,
    )
    worker.status = "idle"
    worker.current_task = None
    worker.error = "boom"
    worker._manual_only_pause = 0
    assert handle.state() == WorkerState(
        status="idle",
        current_task=None,
        error="boom",
        paused_manually=False,
    )


def test_spawn_reads_the_mode_from_the_process_environment(monkeypatch):
    # §10: the mode is configuration, so an operator who exports the flag must
    # not also have to thread it through the spawn call.
    monkeypatch.setenv(WORKER_RUNTIME_MODE_ENV, MODE_PROCESS)
    handle = WorkerRuntimeHandle.spawn(FakeWorker())
    try:
        assert handle.mode == MODE_PROCESS
    finally:
        handle.close()
    monkeypatch.delenv(WORKER_RUNTIME_MODE_ENV)
    assert WorkerRuntimeHandle.spawn(FakeWorker()).mode == MODE_THREAD


def test_process_mode_is_accepted_in_s2():
    # §10: S2 accepts the mode. Construction must not spawn a process, so the
    # spec and the runtime stay unset until the first dispatch_query (§7).
    spawned = WorkerRuntimeHandle.spawn(
        FakeWorker(), environ={WORKER_RUNTIME_MODE_ENV: MODE_PROCESS}
    )
    try:
        assert spawned.mode == MODE_PROCESS
        assert spawned._spec is None
        assert spawned._process_runtime is None
    finally:
        spawned.close()
    direct = WorkerRuntimeHandle(FakeWorker(), mode=MODE_PROCESS)
    try:
        assert direct.mode == MODE_PROCESS
        assert direct._spec is None
        assert direct._process_runtime is None
    finally:
        direct.close()


def test_an_unknown_mode_is_refused_by_the_constructor_too():
    with pytest.raises(InvalidRuntimeModeError):
        WorkerRuntimeHandle(FakeWorker(), mode="udp")


def test_the_seam_holds_a_real_worker_thread_that_was_never_started(tmp_path):
    # The duck-typed contract the handle relies on, pinned against the REAL
    # class rather than a fake: never started, no container, no process.
    worker = WorkerThread(
        name="w-proto",
        definition={"name": "w-proto", "system_prompt": "hi"},
        agent_config={},
        workspace_dir=tmp_path,
        session_id="s",
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )
    handle = WorkerRuntimeHandle.spawn(worker, environ={})
    assert worker.is_alive() is False
    assert handle.state() == WorkerState(
        status="ready",
        current_task=None,
        error=None,
        paused_manually=False,
    )
    assert callable(handle.cancel)
    assert callable(handle.dispatch_query)


def test_the_handle_holds_the_worker_it_was_given():
    worker = FakeWorker()
    assert WorkerRuntimeHandle(worker)._worker is worker
