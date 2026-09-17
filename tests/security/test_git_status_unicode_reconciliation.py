"""RED-first reconciliation test: NFC/NFD phantom untracked + deletion pair.

RECONSTRUCTION. The reporter's original reproduction artifact is unavailable,
so the scenario below is reconstructed from the reported observations rather
than copied verbatim.

Scenario. A repository whose INDEX holds the NFC spelling of a filename
("caf\u00e9.txt" == bytes b"caf\xc3\xa9.txt") while the WORKTREE holds the
byte-distinct but logically-equal NFD spelling ("cafe\u0301.txt" == bytes
b"caf\x65\xcc\x81.txt"). Today ``git status --porcelain=v1`` reports BOTH a
worktree deletion of the tracked NFC path AND a phantom untracked entry for
the NFD path -- one logical file, two spurious rows.

Observed today (reporter bytes)::

    b' D "caf\\303\\251.txt"\\n?? "cafe\\314\\201.txt"\\n'

Post-fix contract asserted here:

* NO ``??`` entry whose NFC-normalized path equals the tracked NFC path.
* NO `` D`` entry for that tracked NFC path.
* A genuinely untracked LONE file still appears as ``??`` -- the
  reconciliation must not over-suppress (guardrail G4).

This module is RED-first: it FAILS today and must PASS once
``tools/git_info_tool.py`` classifies status paths by NFC-normalized identity.

NOTE: ``tests/security`` has no ``__init__.py``; all helpers are local.
"""

import json
import os
import shutil
import subprocess
import unicodedata

import pytest

from tools.git_info_tool import GitInfoTool, _reconcile_status_paths


# Workspace id bound by the host-path status calls below; its provisioned
# config (written by ``allow_host_resources_gate``) permits host resources.
HOST_TEST_WS = "host-test-ws"

# 7-key permission dict (mirrors tests/security/test_reg_contract_matrix.py).
FULL_PERMISSIONS = {
    "git": "write",
    "container": False,
    "network": "outbound",
    "filesystem": "read",
    "system": "read",
    "execution": "banned",
    "mcp": "banned",
}

# The SAME logical filename ("caf\u00e9.txt") in its two Unicode spellings.
NFC_NAME = "caf\u00e9.txt"        # 'e' + U+00E9 (composed)
NFD_NAME = "cafe\u0301.txt"       # 'e' + U+0301 (decomposed; visually identical)
NFC_BYTES = b"caf\xc3\xa9.txt"
NFD_BYTES = b"caf\x65\xcc\x81.txt"

# A genuinely untracked file with no tracked counterpart: must survive.
LONE_BYTES = b"zzz_untracked_lone.txt"


# ---------------------------------------------------------------------------
# Fixtures + helpers (mirror test_reg_contract_matrix.py conventions)
# ---------------------------------------------------------------------------


@pytest.fixture
def allow_host_resources_gate(tmp_path, monkeypatch):
    """Provision an ``allow_host_resources`` config for ``HOST_TEST_WS``.

    Host-side git is fail-CLOSED on an unbound workspace id, so the host-path
    calls below bind ``HOST_TEST_WS`` and this fixture grants that workspace an
    ``allow_host_resources: true`` config.
    """
    vault = tmp_path / "_host_gate_vault"
    cfg_dir = vault / "workspaces" / HOST_TEST_WS
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"allow_host_resources": True}), encoding="utf-8"
    )
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))


_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "HOME": "/dev/null",
}


def _run_git_clean(cwd, *args):
    """Run git with a fully sanitized environment (text mode)."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=dict(_ENV),
        capture_output=True, text=True,
    )


def _run_git_bytes(cwd, *args):
    """Run git with a fully sanitized environment (raw bytes mode)."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=dict(_ENV),
        capture_output=True,
    )


def _init_repo(repo):
    """``git init`` plus a repo-local identity so commits run anywhere."""
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.com"),
    ):
        r = _run_git_clean(repo, *args)
        assert r.returncode == 0, r.stderr


def _build_phantom_repo(base):
    """Reconstruct the NFC-index / NFD-worktree mismatch.

    Returns ``(repo_path, repo_path_bytes)``.
    """
    repo = base / "workspace" / "repo"
    repo_b = os.fsencode(str(repo))
    _init_repo(repo)

    # 1. Track the NFC spelling: create it on disk, stage, commit. The index
    #    and HEAD now hold the NFC bytes.
    with open(os.path.join(repo_b, NFC_BYTES), "wb") as fh:
        fh.write(b"tracked content\n")
    add = _run_git_clean(repo, "add", "--", NFC_NAME)
    assert add.returncode == 0, add.stderr
    commit = _run_git_clean(repo, "commit", "-q", "-m", "track NFC-spelled file")
    assert commit.returncode == 0, commit.stderr

    # 2. Swap the on-disk spelling to NFD (same logical name). The index keeps
    #    NFC; the worktree now holds NFD.
    os.remove(os.path.join(repo_b, NFC_BYTES))
    with open(os.path.join(repo_b, NFD_BYTES), "wb") as fh:
        fh.write(b"tracked content\n")
    return repo, repo_b


def _guard_disk_is_byte_preserving(repo_b, expected_bytes=NFD_BYTES, spelling="NFD"):
    """Skip (never fail) if the filesystem normalized the on-disk name.

    A byte-preserving filesystem is a precondition for the reconstruction; a
    normalizing FS makes the scenario unbuildable, which is an environment
    limitation, not a defect. The observed bytes are recorded in the reason.
    """
    names = os.listdir(repo_b)  # bytes entries (the argument was bytes)
    if expected_bytes not in names:
        observed = sorted(n for n in names if b"caf" in n)
        pytest.skip(
            f"filesystem normalized the reconstructed on-disk {spelling} name; "
            f"expected bytes {expected_bytes!r}, observed caf-family entries "
            f"{observed!r}"
        )
    decoded = expected_bytes.decode("utf-8")
    if not unicodedata.is_normalized(spelling, decoded):
        pytest.skip(
            f"reconstructed {spelling} bytes {expected_bytes!r} are not "
            f"{spelling}-normalized ({decoded!r})"
        )


def _guard_index_keeps(repo, expected_bytes=NFC_BYTES):
    """Skip (never fail) if the git index did not preserve the bytes."""
    out = _run_git_bytes(repo, "ls-files", "-z")
    assert out.returncode == 0, out.stderr
    entries = [e for e in out.stdout.split(b"\x00") if e]
    if expected_bytes not in entries:
        pytest.skip(
            "git index did not preserve the expected spelling; expected "
            f"{expected_bytes!r} among index entries {entries!r}"
        )


def _porcelain_path_bytes(rest):
    """Decode a porcelain-v1 path token to raw bytes.

    ``core.quotepath`` (default true) C-quotes non-ASCII paths, e.g.
    ``"caf\\303\\251.txt"``; the post-fix path may emit raw UTF-8. Both forms
    are handled so the assertion is robust to either classification.
    """
    if rest.startswith('"') and rest.endswith('"'):
        inner = rest[1:-1]
        return (
            inner.encode("latin-1")
            .decode("unicode_escape")
            .encode("latin-1")
        )
    return rest.encode("utf-8")


def _nfc(path_bytes):
    """NFC-normalize a path given as raw bytes; returns bytes."""
    return unicodedata.normalize("NFC", path_bytes.decode("utf-8")).encode("utf-8")


def _status_lines(output):
    """Yield ``(status, path_bytes, raw_line)`` for porcelain rows.

    Trailer lines appended by ``GitInfoTool._with_mode`` are skipped.
    """
    for line in output.splitlines():
        if line.startswith(("execution_mode:", "failure_reason:", "fallback_used:")):
            continue
        if len(line) < 4 or line[2] != " ":
            continue
        status = line[:2]
        try:
            path_bytes = _porcelain_path_bytes(line[3:])
        except Exception:
            continue
        yield status, path_bytes, line


def _host_status(repo):
    """Run ``GitInfoTool(operation='status')`` on the host path."""
    tool = GitInfoTool(
        operation="status",
        working_dir=str(repo),
        workspace_id=HOST_TEST_WS,
        session_permissions=FULL_PERMISSIONS,
    )
    return tool.execute()


# ---------------------------------------------------------------------------
# Tests -- the git-absence precondition is a pytest.skip (never a fail), so a
# FAIL here always means the real reconciliation bug.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("allow_host_resources_gate")
def test_nfc_nfd_pair_is_reconciled_to_one_logical_path(tmp_path):
    """RED today: one logical file must not yield a phantom '??' + ' D' pair."""
    if shutil.which("git") is None:
        pytest.skip(
            "missing git binary on PATH (git): cannot reconstruct the phantom "
            "NFC/NFD repository without a real git."
        )
    repo, repo_b = _build_phantom_repo(tmp_path)
    _guard_disk_is_byte_preserving(repo_b)
    _guard_index_keeps(repo)

    output = _host_status(repo)
    tracked_nfc = _nfc(NFC_BYTES)
    rows = list(_status_lines(output))

    phantoms = [
        (pb, ln) for st, pb, ln in rows if st == "??" and _nfc(pb) == tracked_nfc
    ]
    deletions = [
        (pb, ln) for st, pb, ln in rows if st == " D" and _nfc(pb) == tracked_nfc
    ]

    assert not phantoms, (
        "phantom untracked entry for the tracked (NFC) path "
        f"{NFC_BYTES!r}: {phantoms!r}\nfull status output:\n{output}"
    )
    assert not deletions, (
        "phantom worktree deletion of the tracked NFC path "
        f"{NFC_BYTES!r}: {deletions!r}\nfull status output:\n{output}"
    )


@pytest.mark.usefixtures("allow_host_resources_gate")
def test_genuinely_untracked_lone_file_still_reported(tmp_path):
    """PASS today (guardrail G4): a real lone untracked file is still '??'."""
    if shutil.which("git") is None:
        pytest.skip(
            "missing git binary on PATH (git): cannot reconstruct the phantom "
            "NFC/NFD repository without a real git."
        )
    repo, repo_b = _build_phantom_repo(tmp_path)
    _guard_disk_is_byte_preserving(repo_b)

    with open(os.path.join(repo_b, LONE_BYTES), "wb") as fh:
        fh.write(b"lone\n")

    output = _host_status(repo)
    rows = list(_status_lines(output))
    untracked = [ln for st, pb, ln in rows if st == "??" and pb == LONE_BYTES]

    assert untracked, (
        f"a genuinely untracked lone file ({LONE_BYTES!r}) must still be "
        f"reported as '??'; none found.\nfull status output:\n{output}"
    )



def _build_phantom_repo_mirrored(base):
    """Mirror of ``_build_phantom_repo``: NFD in the INDEX, NFC on disk.

    Returns ``(repo_path, repo_path_bytes)``.
    """
    repo = base / "workspace" / "repo"
    repo_b = os.fsencode(str(repo))
    _init_repo(repo)

    # 1. Track the NFD spelling: index and HEAD hold the NFD bytes.
    with open(os.path.join(repo_b, NFD_BYTES), "wb") as fh:
        fh.write(b"tracked content\n")
    add = _run_git_clean(repo, "add", "--", NFD_NAME)
    assert add.returncode == 0, add.stderr
    commit = _run_git_clean(repo, "commit", "-q", "-m", "track NFD-spelled file")
    assert commit.returncode == 0, commit.stderr

    # 2. Swap the on-disk spelling to NFC (same logical name).
    os.remove(os.path.join(repo_b, NFD_BYTES))
    with open(os.path.join(repo_b, NFC_BYTES), "wb") as fh:
        fh.write(b"tracked content\n")
    return repo, repo_b


@pytest.mark.usefixtures("allow_host_resources_gate")
def test_mirrored_nfd_index_nfc_worktree_pair_is_reconciled(tmp_path):
    """Mirror of Test A: NFD in the INDEX, NFC on disk -- the symmetry proof.

    The fix must collapse the pair in BOTH directions; an asymmetric
    implementation (normalizing only one side) passes Test A and fails here.
    """
    if shutil.which("git") is None:
        pytest.skip(
            "missing git binary on PATH (git): cannot reconstruct the phantom "
            "NFC/NFD repository without a real git."
        )
    repo, repo_b = _build_phantom_repo_mirrored(tmp_path)
    _guard_disk_is_byte_preserving(repo_b, expected_bytes=NFC_BYTES, spelling="NFC")
    _guard_index_keeps(repo, expected_bytes=NFD_BYTES)

    output = _host_status(repo)
    tracked_nfc = _nfc(NFD_BYTES)  # NFC of the tracked (NFD) spelling
    rows = list(_status_lines(output))

    phantoms = [
        (pb, ln) for st, pb, ln in rows if st == "??" and _nfc(pb) == tracked_nfc
    ]
    deletions = [
        (pb, ln) for st, pb, ln in rows if st == " D" and _nfc(pb) == tracked_nfc
    ]

    assert not phantoms, (
        "phantom untracked entry for the tracked (NFD) path "
        f"{NFD_BYTES!r}: {phantoms!r}\nfull status output:\n{output}"
    )
    assert not deletions, (
        "phantom worktree deletion of the tracked NFD path "
        f"{NFD_BYTES!r}: {deletions!r}\nfull status output:\n{output}"
    )


def test_reconciliation_parses_quoted_and_unquoted_porcelain_forms():
    """Pure-parser test (needs NO git): BOTH path forms must reconcile.

    ``-c core.quotepath=false`` makes git emit raw UTF-8 paths; when that flag
    is unavailable the same rows arrive C-quoted. The classifier must collapse
    the equivalent pair in EITHER form.
    """
    quoted = ' D "caf\\303\\251.txt"\n?? "cafe\\314\\201.txt"\n'
    unquoted = " D caf\u00e9.txt\n?? cafe\u0301.txt\n"
    for label, raw in (("quoted", quoted), ("unquoted", unquoted)):
        out = _reconcile_status_paths(raw)
        rows = [(st, ln) for st, _pb, ln in _status_lines(out)]
        assert not [ln for st, ln in rows if st == "??"], (
            f"{label} form: phantom '??' survived reconciliation: {rows!r}"
        )
        assert not [ln for st, ln in rows if st == " D"], (
            f"{label} form: phantom ' D' survived reconciliation: {rows!r}"
        )

