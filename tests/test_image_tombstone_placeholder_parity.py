"""Pin test for the "image unavailable" sentence across the Py/JS boundary.

The retention pass (``session.image_retention``) empties ``message['images']``
and leaves an integer count under ``TOMBSTONE_COUNT_KEY`` (``images_tombstoned``).
Two independent readers then have to say the same thing about that message:

1. the provider adapters, which substitute
   ``agent.core.image_store.IMAGE_UNAVAILABLE_PLACEHOLDER`` so the MODEL knows
   the screenshot is gone (``llm_providers/openai_compatible.py`` and
   ``llm_providers/anthropic_provider.py``);
2. the transcript UI, ``web_ui/frontend/src/components/chat/MessageBubble.jsx``
   (``MessageImages``), which has to render the same sentence so the USER is
   told the same thing.

The JSX is a separate runtime: it cannot import a Python constant, and there is
no build-time bridge for this string.  So the sentence is a hand-copied literal
in the JSX and THIS TEST is the seam that keeps the copy honest -- if either
side is reworded alone, one of these assertions fails.

Hermetic: both sides are read as text; nothing is imported from the frontend and
no browser/node toolchain is required.
"""

import os
import re

from agent.core.image_store import IMAGE_UNAVAILABLE_PLACEHOLDER

_MESSAGE_BUBBLE_JSX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "web_ui",
    "frontend",
    "src",
    "components",
    "chat",
    "MessageBubble.jsx",
)

#: The rendered hint, anchored on its test id so a different string elsewhere in
#: the file (a comment, an alt text) cannot satisfy the extraction.
_HINT_TEXT_RE = re.compile(
    r'data-testid="message-image-tombstoned"\s*>\s*(?P<text>[^\n]+?)\s*</span>'
)


def _read_message_bubble() -> str:
    with open(_MESSAGE_BUBBLE_JSX, "r", encoding="utf-8") as fh:
        return fh.read()


def _empty_branch_end(body: str, guard: int) -> int:
    """Offset of the ``return null`` that closes the empty-images branch.

    Matched as a STATEMENT (line-initial) so the phrase in the comment above it
    cannot be mistaken for it.
    """
    match = re.compile(r"^\s*return null\b", re.MULTILINE).search(body, guard)
    assert match is not None, "the empty-images branch must still end in `return null`"
    return match.start()


def _message_images_body() -> str:
    """The source of ``function MessageImages(...)`` only.

    Sliced rather than parsed: the assertions below are about the shape of that
    one component, and a whole-file search would pass on a hint rendered from
    somewhere else.
    """
    text = _read_message_bubble()
    start = text.index("function MessageImages(")
    end = text.index("── Route content based on role", start)
    return text[start:end]


# ==========================================================================
# 1. The JSX says the sentence at all
# ==========================================================================
def test_jsx_renders_the_unavailable_sentence():
    body = _message_images_body()
    match = _HINT_TEXT_RE.search(body)
    assert match is not None, (
        "MessageImages must render the tombstone hint with "
        'data-testid="message-image-tombstoned"'
    )
    assert match.group("text").strip()


# ==========================================================================
# 2. The JSX literal equals the Python owner (drift either way fails)
# ==========================================================================
def test_jsx_sentence_matches_the_python_placeholder():
    """The JSX hint text must equal ``IMAGE_UNAVAILABLE_PLACEHOLDER``.

    That constant (``agent/core/image_store.py``) is what both provider adapters
    put into the wire message, so this equality is what makes the model and the
    user read the same sentence.
    """
    match = _HINT_TEXT_RE.search(_message_images_body())
    assert match is not None
    jsx_sentence = match.group("text").strip()

    assert jsx_sentence == IMAGE_UNAVAILABLE_PLACEHOLDER, (
        "the transcript hint drifted from the adapter placeholder: "
        f"{jsx_sentence!r} != {IMAGE_UNAVAILABLE_PLACEHOLDER!r}"
    )


# ==========================================================================
# 3. The hint is gated: tombstone count > 0 AND no live images
# ==========================================================================
def test_hint_is_gated_on_tombstone_count_and_empty_images():
    body = _message_images_body()

    assert re.search(r"images\.length\s*===\s*0", body), (
        "MessageImages must keep the empty-images guard"
    )
    assert re.search(r"images_tombstoned[^\n]*>\s*0", body), (
        "the hint must be gated on a positive images_tombstoned count"
    )

    guard = body.index("if (images.length === 0)")
    hint = body.index('data-testid="message-image-tombstoned"')
    empty_branch_end = _empty_branch_end(body, guard)

    assert guard < hint < empty_branch_end, (
        "the hint must be rendered INSIDE the images.length === 0 branch: "
        f"guard@{guard} hint@{hint} empty-branch-end@{empty_branch_end}"
    )


def test_message_with_live_images_never_reaches_the_hint():
    """Negative case: a message that still carries images renders as before.

    The live-image path (``images.map(...)``) sits AFTER the empty-images
    branch, so a message with live images cannot fall into the hint; and the
    hint literal appears only inside that branch.  A hoisted hint (e.g. one
    rendered beside the thumbnails) would break the ordering asserted here.
    """
    body = _message_images_body()

    start = body.index("if (images.length === 0)")
    empty_branch_end = _empty_branch_end(body, start)
    live_path = body.index("images.map(")
    assert live_path > empty_branch_end, (
        "the live-image render path must follow the empty-images branch"
    )

    first_hint = body.index('data-testid="message-image-tombstoned"', start)
    last_hint = body.rindex('data-testid="message-image-tombstoned"')
    assert start < first_hint <= last_hint < empty_branch_end, (
        "every tombstone-hint occurrence must stay inside the empty-images branch"
    )
