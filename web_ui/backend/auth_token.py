"""auth_token.py -- the Web UI's shared secret: one file, one policy.

The web UI used to authenticate with nothing at all.  The bind was loopback by
default and ``--allow-remote-bind`` carried a warning instead of a check, so
every request that reached the port could drive this machine's agent.  The
replacement is split in two on purpose:

* :mod:`web_ui.backend.auth_boundary` decides *who may in* on every request,
* this module owns the *storage* of the secret that decision compares against.

Keeping the store in its own module means the enforcement half has exactly one
question to ask ("what is the token?") and the answer has exactly one
implementation, so a test can pin the storage policy without standing up an
ASGI stack, and the boundary can be tested with a literal token.

Storage policy
--------------
* **Location:** ``<vault>/state/web_ui_token``.  Inside the vault, so it is
  covered by the vault's backup / repair story and by the existing rule that
  vault content is never served or written by a browser-reachable route.
* **Content:** 32 bytes from :func:`secrets.token_bytes` (256 bits, the same
  order as the HMAC-SHA256 the boundary compares against), hex-encoded to 64
  ASCII characters.  Hex rather than base64/urlsafe-base64 so the value can be
  pasted into a shell, an ``Authorization`` header or a ``.env`` file without
  quoting surprises.
* **Creation:** ``os.open(..., O_WRONLY | O_CREAT | O_EXCL, 0o600)``.  ``O_EXCL``
  makes "does it exist?" and "create it" one atomic step, so two servers
  starting at the same moment cannot each install a different token and then
  disagree about which cookies are valid.  The mode argument is an upper bound:
  ``umask`` can only clear bits, never add them, so the file is never wider
  than ``0o600``.
* **State directory:** created ``0o700`` for the same reason -- the file mode
  is meaningless if the directory can be listed by group/other.
* **Verify, don't rewrite.**  An existing token is *read*, never regenerated,
  and its permissions are *audited*, never silently changed.  Regenerating a
  token invalidates every cookie already issued (the running frontend drops to
  a login it cannot complete), and ``chmod``-ing a file this module did not
  create can break a deliberate setup (a shared volume, an ACL-managed parent,
  a container bind-mount).  A mode wider than ``0o600`` is therefore reported
  once, as a ``WARNING`` that names the path, the observed mode and the exact
  ``chmod`` command that fixes it -- loud, falsifiable, and not a rewrite.
  This is the residual of the policy: a pre-existing world-readable token is
  visible to other local users until the operator acts on the warning.

Fail closed
-----------
A token file that exists but cannot be used (unreadable, empty, not ASCII)
raises :class:`RuntimeError` instead of being replaced.  A broken credential
store must be loud: minting a fresh secret would both hide the damage and lock
out a frontend that is already holding a valid cookie.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Optional

from thoughtmachine.vault import vault_root

logger = logging.getLogger(__name__)

#: Random bytes behind the token.  32 bytes = 256 bits; guessing the value is
#: not the interesting attack, which is exactly why the *storage* gets the
#: attention and the length does not.
TOKEN_BYTES = 32

#: File name inside ``<vault>/state/``.
TOKEN_FILENAME = "web_ui_token"

#: Vault compartment that holds machine-managed runtime state.
STATE_DIRNAME = "state"

#: Owner-only, for both the directory and the file.
STATE_DIR_MODE = 0o700
TOKEN_FILE_MODE = 0o600

#: Group/other permission bits.  Non-zero on the directory OR the file means
#: a user other than the owner can reach the token.
_WIDE_MASK = 0o077

#: POSIX permission bits are emulated on Windows (ACLs are the real control),
#: so the audit is a no-op there rather than a permanent false warning.
_AUDIT_MODES = os.name != "nt"


def token_path() -> Path:
    """Absolute path of the token file: ``<vault>/state/web_ui_token``."""
    return Path(vault_root()) / STATE_DIRNAME / TOKEN_FILENAME


def read_token(path: Optional[Path] = None) -> Optional[str]:
    """Return the stored token, or ``None`` when there is no token file.

    Raises :class:`RuntimeError` when the file exists but cannot be used
    (unreadable, non-ASCII, or blank).  See the module docstring: a broken
    credential store fails closed and loud instead of being replaced.
    """
    target = Path(path) if path is not None else token_path()
    try:
        raw = target.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"web UI token file {target} is unreadable: {exc}"
        ) from exc
    token = raw.strip()
    if not token:
        raise RuntimeError(
            f"web UI token file {target} is empty; delete it to have a fresh "
            f"token minted on the next start"
        )
    return token


def ensure_token(path: Optional[Path] = None) -> str:
    """Return the token, minting it exactly once.

    Idempotent by construction: an existing file is verified and returned
    unchanged, so restarting the server, importing this module twice or running
    two instances all agree on one secret.  Only a *missing* file creates a
    token, and the creation is exclusive (``O_CREAT | O_EXCL``) so a lost race
    yields the winner's token rather than a second, silently different one.
    """
    target = Path(path) if path is not None else token_path()

    token = read_token(target)
    if token is not None:
        _audit_permissions(target, TOKEN_FILE_MODE)
        return token

    _ensure_state_dir(target.parent)

    created = _create_exclusively(target)
    if created is not None:
        logger.info("auth_token: minted a new web UI token at %s", target)
        return created

    # Another process created the file between our read and our create.  Its
    # token is the token: overwriting it would invalidate the cookie that
    # process may already have handed out.
    token = read_token(target)
    if token is None:
        raise RuntimeError(
            f"web UI token file {target} disappeared while it was being created"
        )
    _audit_permissions(target, TOKEN_FILE_MODE)
    return token


def _ensure_state_dir(directory: Path) -> None:
    """Create ``<vault>/state`` with mode ``0o700``; audit, never chmod."""
    try:
        directory.mkdir(mode=STATE_DIR_MODE, parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"cannot create the web UI state directory {directory}: {exc}"
        ) from exc
    _audit_permissions(directory, STATE_DIR_MODE)


def _create_exclusively(target: Path) -> Optional[str]:
    """Create *target* atomically with a fresh token; ``None`` if it existed."""
    token = secrets.token_bytes(TOKEN_BYTES).hex()
    try:
        fd = os.open(
            str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, TOKEN_FILE_MODE
        )
    except FileExistsError:
        return None
    except OSError as exc:
        raise RuntimeError(
            f"cannot create the web UI token file {target}: {exc}"
        ) from exc
    try:
        os.write(fd, (token + "\n").encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return token


def _audit_permissions(path: Path, expected_mode: int) -> Optional[str]:
    """Warn when *path* is reachable by group/other; return the warning text.

    Never changes the mode -- see "Verify, don't rewrite" in the module
    docstring.  Never raises: an audit that can fail a request would be a worse
    bug than the one it reports.
    """
    if not _AUDIT_MODES:
        return None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if not mode & _WIDE_MASK:
        return None
    message = (
        f"auth_token: {path} has mode 0o{mode:03o} (expected 0o{expected_mode:03o}); "
        f"the web UI credential is reachable by other local users. This module "
        f"does not rewrite permissions it did not set -- run "
        f"`chmod {expected_mode:o} {path}` to fix it."
    )
    logger.warning(message)
    return message
