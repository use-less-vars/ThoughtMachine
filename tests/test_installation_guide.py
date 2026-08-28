"""Checks that docs/installation_guide.md exists and covers the documented
platform surface (Linux x86_64, Windows x64, macOS unsupported), the
one-command Linux installer, the --check-only flag, the onboarding wizard,
and links to at least one of the Windows-specific docs.

These are content sanity checks, not a render test: they assert that the
guide would answer the questions a user actually asks ("which platforms are
supported?", "how do I install?", "what do I do on first run?").
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "docs" / "installation_guide.md"


def test_guide_exists_and_is_substantial():
    assert GUIDE.is_file(), f"missing {GUIDE.relative_to(REPO_ROOT)}"
    text = GUIDE.read_text(encoding="utf-8")
    # Non-trivial: a real guide, not a stub.
    assert len(text) > 2000, "guide looks like a stub"
    assert len(text.splitlines()) > 60, "guide looks too short"


def test_platform_surface_covered():
    text = GUIDE.read_text(encoding="utf-8")
    assert "Linux x86_64" in text
    assert "Windows x64" in text
    # macOS must be explicitly declared unsupported, not silently omitted.
    assert "macOS" in text
    assert "Not supported" in text or "not supported" in text


def test_linux_installer_and_check_only_mentioned():
    text = GUIDE.read_text(encoding="utf-8")
    assert "install.sh" in text
    # The one-command installer claim: chmod +x install.sh && ./install.sh
    assert "./install.sh" in text
    assert "--check-only" in text
    assert "start_thoughtmachine.sh" in text


def test_first_run_wizard_mentioned():
    text = GUIDE.read_text(encoding="utf-8")
    assert "wizard" in text.lower()
    assert "onboarding" in text.lower()


def test_links_to_windows_docs():
    import re

    text = GUIDE.read_text(encoding="utf-8")
    linked = "windows_installation_saga.md" in text or "windows_stability_contract.md" in text
    assert linked, "guide must link to windows_installation_saga.md or windows_stability_contract.md"
    # Any relative markdown links in the guide must resolve to real files
    # (links are relative to the guide's own directory, docs/).
    for target in re.findall(r"\]\(([^)#]+\.md)\)", text):
        if target.startswith(("http://", "https://")):
            continue
        assert (GUIDE.parent / target).is_file(), f"broken link in guide: {target}"
