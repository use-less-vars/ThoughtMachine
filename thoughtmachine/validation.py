"""
validation.py — Shared validators for vault-scoped path segments.

Vault-scoped names -- credential names, workspace ids -- become a single path
segment under the vault root (``<vault>/credentials/global/<name>``,
``<vault>/workspaces/<id>``).  A name that is not one segment escapes the
namespace it was scoped to: ``..`` lands on the vault root, ``a/b`` lands two
levels out, and on Windows a backslash does the same.

This module is the one authority for that rule (Inv.6).  It lives in
``thoughtmachine/`` rather than in ``web_ui/backend/global_routes.py``, where
the credential check started, because ``thoughtmachine/`` must not depend on
the HTTP layer: ``global_routes`` imports ``fastapi``, ``pydantic``,
``session``, ``agent`` and ``tools``, while ``workspace_capabilities`` is
imported by the bootstrap path before any of those exist.

Both call sites get an error *message* or ``None`` from here and choose their
own failure mode: the credential routes return the message to the caller,
``workspace_capabilities._workspace_dir`` raises ``ValueError``.
"""

from __future__ import annotations

from typing import Optional

#: Longest identifier accepted for a vault-scoped path segment.
MAX_SEGMENT_LENGTH = 128


def validate_single_path_segment(
    name: str,
    *,
    kind: str,
    max_length: int = MAX_SEGMENT_LENGTH,
) -> Optional[str]:
    """Return an error message if *name* is not a single safe path segment.

    ``kind`` names the thing being validated and is interpolated into the
    message verbatim, so each caller keeps its existing wording
    ("credential name must not be empty").  ``None`` means acceptable.
    """
    if not name:
        return f"{kind} must not be empty"
    if name == "." or ".." in name or "/" in name or "\\" in name:
        return f"{kind} must be a single path segment"
    if len(name) > max_length:
        return f"{kind} must be at most {max_length} characters"
    return None
