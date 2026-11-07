"""Secret-bearing string wrapper.

``Secret`` is a :class:`str` subclass whose *display* forms are redacted::

    key = Secret("sk-live-abc")
    f"{key}"        # '<secret>'
    str(key)        # '<secret>'
    "%s" % key      # '<secret>'
    repr(key)       # '<secret>'
    print(key)      # '<secret>'
    repr({"k": key})# "{'k': <secret>}"

Interpolating the value into a log line, an f-string, a ``print()`` or a
container ``repr()`` therefore cannot reveal it -- redaction is a property of
the *class*, not of each call site.

Comparisons and membership keep working on the underlying value (``key ==
"sk-live-abc"`` is ``True``, ``"abc" in key`` is ``True``), so existing
truthiness / equality / ``in`` checks need no change.

The only way to obtain the raw value is the explicit :meth:`Secret.reveal`
(module helper :func:`reveal_secret`).  That is deliberate: every reveal call
is a grep-able audit point.

Known limits (the wrapper is not a sandbox):

* ``json.dumps`` / ``json.dumps``-based serialisers call the ``str`` *content*
  directly (they bypass ``__str__``/``__format__``), so JSON output of a
  ``Secret`` carries the raw value.
* ``str.__str__(secret)``, ``secret.encode()``, ``"" + secret``, ``.split()``
  etc. return the raw value because they operate on the ``str`` payload.
* Assignment onto a pydantic model field (``config.api_key = "sk-..."``) and
  ``model_copy(update=...)`` bypass validation, so the field can hold a plain
  ``str`` afterwards.
"""

from __future__ import annotations

from typing import Any

__all__ = ["Secret", "reveal_secret", "REDACTED"]

#: Marker used by the wrapper's display forms.
REDACTED = "<secret>"


class Secret(str):
    """A string that redacts itself in every display context."""

    __slots__ = ()

    def __str__(self) -> str:  # noqa: D105 - handled by class docstring
        return REDACTED

    def __repr__(self) -> str:
        return REDACTED

    def __format__(self, format_spec: str) -> str:
        # f"{secret}" / "{secret:>20}" / "{}".format(secret)
        return format(REDACTED, format_spec)

    def reveal(self) -> str:
        """Return the raw secret value.

        This is the ONLY supported way to un-redact.  Every call site is an
        audit point for "where does the raw key cross into a third-party
        client / HTTP call".
        """
        return str.__str__(self)


def reveal_secret(value: Any) -> Any:
    """Return the raw value of *value* if it is a :class:`Secret`.

    Non-``Secret`` values are returned unchanged, so call sites that also
    accept plain strings (e.g. a provider built directly with a raw key) keep
    working.
    """
    if isinstance(value, Secret):
        return value.reveal()
    return value
