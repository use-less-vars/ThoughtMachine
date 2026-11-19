"""Log hygiene for pasted images: inline payloads never reach the log file.

A pasted screenshot is delivered to the backend as a base64 data URL and the
provider adapter inlines it into the request's message content.  The structured
agent log records both the query (``log_agent_start``) and the outgoing request
(``log_llm_request``), so without masking the whole payload would be written
verbatim into ``agent_<session>.jsonl`` -- megabytes per turn, and a copy of
the user's screen on disk.

Covers:
(a) The module-level helpers: data-URL redaction, length bounding, and
    multimodal-content masking.
(b) ``log_agent_start`` bounds the query and redacts any data URL in it.
(c) ``log_llm_request`` masks ``image_url`` parts in ``content`` (a list, which
    the previous ``_truncate_string`` call raised ``TypeError`` on) and in the
    DEBUG ``full_messages`` copy, and bounds long text parts.
(d) The caller's message list is never mutated by logging.

Hermetic by construction: tmp_path log dir, no vault, no network.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from agent.logging import (
    QUERY_LOG_LIMIT,
    AgentLogger,
    _bounded_text,
    _mask_content_for_log,
    _redact_data_urls,
)

#: A PNG-ish payload large enough to matter but small enough to stay fast.
_IMAGE_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00\x01\x02\x03' * 160
_IMAGE_B64 = base64.b64encode(_IMAGE_BYTES).decode('ascii')
_IMAGE_DATA_URL = f'data:image/png;base64,{_IMAGE_B64}'

#: Exactly the placeholder the helpers must produce for `_IMAGE_DATA_URL`.
_REDACTED_URL = f'data:image/png;base64,<redacted {len(_IMAGE_B64)} chars>'


def _stub_config(**overrides) -> SimpleNamespace:
    """Minimal logger config (mirrors ``agent/config/models.py`` defaults)."""
    cfg = SimpleNamespace(
        enable_logging=True,
        enable_file_logging=True,
        jsonl_format=True,
        max_file_size_mb=10,
        log_level="DEBUG",
        file_log_level="DEBUG",
        log_categories=["SESSION", "LLM", "TOOLS"],
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Log into tmp_path only; neutralise env overrides; close file handles."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENT_LOG_CATEGORIES", raising=False)
    monkeypatch.delenv("TM_LOG_FILE_LEVEL", raising=False)
    created: list = []
    yield tmp_path, created
    for logger in created:
        handle = getattr(logger, "_file_handle", None)
        if handle is not None and not handle.closed:
            handle.close()


def _make(hermetic, **kwargs) -> AgentLogger:
    """Build a logger writing JSONL into the hermetic tmp tree."""
    tmp_path, created = hermetic
    logger = AgentLogger(
        _stub_config(),
        log_dir=str(tmp_path / "logs"),
        enable_file_logging=True,
        jsonl_format=True,
        session_id=kwargs.pop("session_id", "img-log-1"),
        **kwargs,
    )
    created.append(logger)
    return logger


def _records(logger) -> list:
    """Parse every JSONL record the logger has written so far."""
    with open(logger.log_file_path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _raw(logger) -> str:
    """Return the log file's raw text (what would leak onto disk)."""
    with open(logger.log_file_path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# (a) helpers
# ---------------------------------------------------------------------------


def test_redact_data_urls_replaces_the_payload_but_keeps_the_mime():
    redacted = _redact_data_urls(f'look: {_IMAGE_DATA_URL} -- end')

    assert redacted == f'look: {_REDACTED_URL} -- end'
    assert _IMAGE_B64 not in redacted


def test_redact_data_urls_leaves_ordinary_text_alone():
    text = 'why does this test fail? data: not a url, base64 is fine'

    assert _redact_data_urls(text) == text


def test_bounded_text_reports_the_original_length():
    text = 'x' * 300

    bounded = _bounded_text(text, 100)

    assert bounded == 'x' * 100 + '... [truncated, total 300 chars]'


def test_bounded_text_with_zero_limit_still_redacts_image_data():
    bounded = _bounded_text(_IMAGE_DATA_URL, 0)

    assert bounded == _REDACTED_URL


def test_bounded_text_passes_non_strings_through():
    marker = object()

    assert _bounded_text(None, 100) is None
    assert _bounded_text(7, 100) == 7
    assert _bounded_text(marker, 100) is marker


def test_mask_content_for_log_replaces_image_parts():
    content = [
        {'type': 'text', 'text': 'what is wrong with this?'},
        {'type': 'image_url', 'image_url': {'url': _IMAGE_DATA_URL}},
    ]

    masked = _mask_content_for_log(content, 0)

    assert masked[0] == {'type': 'text', 'text': 'what is wrong with this?'}
    assert masked[1] == {'type': 'image_url', 'image_url': {'url': '<image data>'}}
    assert _IMAGE_B64 not in json.dumps(masked)


def test_mask_content_for_log_bounds_text_parts():
    content = [{'type': 'text', 'text': 'y' * 300}]

    masked = _mask_content_for_log(content, 100)

    assert masked[0]['text'] == 'y' * 100 + '... [truncated, total 300 chars]'


def test_mask_content_for_log_handles_plain_strings_and_other_types():
    assert _mask_content_for_log('a' * 300, 100) == 'a' * 100 + '... [truncated, total 300 chars]'
    assert _mask_content_for_log(_IMAGE_DATA_URL, 0) == _REDACTED_URL
    assert _mask_content_for_log(None, 100) is None


# ---------------------------------------------------------------------------
# (b) log_agent_start
# ---------------------------------------------------------------------------


def test_log_agent_start_never_writes_the_image_payload(hermetic):
    logger = _make(hermetic)
    query = f'what is wrong here? {_IMAGE_DATA_URL} ' + 'detail ' * 500

    logger.log_agent_start(query, {'model': 'stub'})

    raw = _raw(logger)
    assert _IMAGE_B64 not in raw
    (record,) = _records(logger)
    logged = record['data']['query']
    assert logged.startswith('what is wrong here? data:image/png;base64,<redacted')
    assert len(logged) <= QUERY_LOG_LIMIT + 100


def test_log_agent_start_keeps_a_short_query_intact(hermetic):
    logger = _make(hermetic)

    logger.log_agent_start('why is the build red?', {})

    (record,) = _records(logger)
    assert record['data']['query'] == 'why is the build red?'


# ---------------------------------------------------------------------------
# (c) log_llm_request
# ---------------------------------------------------------------------------


def test_log_llm_request_accepts_multimodal_content(hermetic):
    """A list-valued ``content`` used to raise on the str-only truncation path."""
    logger = _make(hermetic)
    messages = [{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': 'what is wrong with this?'},
            {'type': 'image_url', 'image_url': {'url': _IMAGE_DATA_URL}},
        ],
    }]

    logger.log_llm_request(messages, [])

    raw = _raw(logger)
    assert _IMAGE_B64 not in raw
    (record,) = _records(logger)
    logged_content = record['data']['messages'][0]['content']
    assert logged_content[0] == {'type': 'text', 'text': 'what is wrong with this?'}
    assert logged_content[1] == {'type': 'image_url', 'image_url': {'url': '<image data>'}}


def test_log_llm_request_masks_the_debug_full_messages_copy(hermetic):
    logger = _make(hermetic)
    messages = [{
        'role': 'user',
        'content': [{'type': 'image_url', 'image_url': {'url': _IMAGE_DATA_URL}}],
    }]

    logger.log_llm_request(messages, [])

    (record,) = _records(logger)
    full = record['data']['full_messages']
    assert full[0]['content'] == [{'type': 'image_url', 'image_url': {'url': '<image data>'}}]
    assert full[0]['role'] == 'user'


def test_log_llm_request_bounds_long_text_content(hermetic, monkeypatch):
    monkeypatch.setenv('CONVERSATION_CONTENT_TRUNCATE_LENGTH', '50')
    logger = _make(hermetic)
    messages = [{'role': 'user', 'content': 'z' * 200}]

    logger.log_llm_request(messages, [])

    (record,) = _records(logger)
    assert record['data']['messages'][0]['content'] == 'z' * 50 + '... [truncated, total 200 chars]'


def test_log_llm_request_does_not_mutate_the_callers_messages(hermetic):
    logger = _make(hermetic)
    image_part = {'type': 'image_url', 'image_url': {'url': _IMAGE_DATA_URL}}
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'q'}, image_part]}]

    logger.log_llm_request(messages, [])

    assert messages[0]['content'][1] is image_part
    assert image_part['image_url']['url'] == _IMAGE_DATA_URL
