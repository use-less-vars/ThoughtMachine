"""Secret redaction for structured log lines.

Applied to the JSON-serialized line *before* it is written to any log file,
so secrets never hit disk.  The redaction is intentionally conservative:
values matching well-known secret shapes (API keys, tokens, passwords) are
replaced with ``<REDACTED>`` while keeping a recognizable prefix (e.g.
``sk-``) so the log remains debuggable.  The replacement is JSON-safe, so
the redacted line stays parseable as JSON.
"""

from __future__ import annotations

import re

# Secret-shaped value patterns.  Group 1 (when present) is the recognizable
# prefix that is preserved; the remainder of the match is replaced.
REDACT_PATTERNS: list[re.Pattern] = [
    # OpenAI-style keys: sk-..., sk-or-..., sk-ant-...
    re.compile(r"(sk-(?:or-|ant-)?)[A-Za-z0-9_\-]{8,}"),
    # GitHub PATs: classic (ghp_, gho_, ghu_, ghs_, ghr_) and fine-grained (gh2_)
    re.compile(r"(gh[pousr2]_)[A-Za-z0-9]{20,}"),
    # Authorization header: "Bearer <token>"
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
    # Bare Bearer token (no trailing space in the preserved prefix)
    re.compile(r"(Bearer)\s+[A-Za-z0-9\-._~+/]+=*"),
    # AWS access key id
    re.compile(r"(AKIA)[0-9A-Z]{16}"),
    # key=value / "key": value / key: value pairs for known secret names
    # (case-insensitive: env-var style API_KEY / SECRET / PASSWORD is common)
    re.compile(
        r'(["\']?(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|password|passwd)["\']?\s*[:=]\s*["\']?)([^"\'\s,}]+)',
        re.IGNORECASE,
    ),
    # PEM private key blocks (whole block replaced)
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL,
    ),
]


def _replace_match(match: re.Match) -> str:
    """Preserve the recognizable prefix (group 1) if present, else full redact."""
    if match.lastindex and match.group(1) is not None:
        return match.group(1) + "<REDACTED>"
    return "<REDACTED>"


def redact(text: str) -> str:
    """Return *text* with secret-shaped substrings replaced by ``<REDACTED>``.

    Never raises: on any unexpected failure the original text is returned
    unchanged so logging can never crash the caller.
    """
    try:
        if not isinstance(text, str):
            text = str(text)
        for pattern in REDACT_PATTERNS:
            text = pattern.sub(_replace_match, text)
        return text
    except Exception:
        try:
            return str(text)
        except Exception:
            return ""
