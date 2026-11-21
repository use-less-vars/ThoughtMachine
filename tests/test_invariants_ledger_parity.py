"""Parity between the invariant ledger and the contract ledger.

`.thoughtmachine/knowledge/project/architecture_invariants.md` lists the rules a
change must not re-derive. `docs/CONTRACTS.md` is where such a rule becomes
merge-blocking: an enforcement site plus a named test that dies if the
enforcement is removed. A rule that lives in one ledger and not the other is
drift, and it drifts silently -- which is the thing this file refuses.

So every `## N. <title>` section of the invariant ledger must carry exactly one
`**Contract:** <value>` line between its heading and the next `## ` heading, and
the value must be either

  * a reference to a `## Contract N:` heading that really exists in
    `docs/CONTRACTS.md` (`Contract 1`, `Contract 1:`, `docs/CONTRACTS.md \u00a71`
    and `docs/CONTRACTS.md, \u00a71` are all accepted), or
  * `GAP -- <reason>` (em dash, en dash or hyphen) with a reason long enough to
    be a statement rather than a shrug.

A `GAP` is not a pass. It is a recorded admission that a rule the ledger calls
load-bearing has no contract behind it; the value of the check is that the
admission has to be written down, attributed and kept current instead of being
absent.

Dependency-free on purpose (pathlib + re only): no docker, no network, no vault,
so it can run as a step of the fast gate on a bare ubuntu-latest runner.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

INVARIANTS_REL = ".thoughtmachine/knowledge/project/architecture_invariants.md"
INVARIANT_LEDGER = REPO_ROOT / ".thoughtmachine" / "knowledge" / "project" / "architecture_invariants.md"

CONTRACTS_REL = "docs/CONTRACTS.md"
CONTRACT_LEDGER = REPO_ROOT / "docs" / "CONTRACTS.md"

#: Rule (c), anti-vacuity floor. A parity test over zero headings passes for the
#: worst possible reason: nothing is wrong because nothing is checked. These are
#: floors on the CONTENT, not on the code, so they are counts of headings -- and
#: they are deliberately equal to what the two ledgers carry today, so that
#: deleting a section turns this red rather than shrinking the check.
#:
#: The brief for this file asked for six invariants. The ledger carries two
#: (\u00a71 container permission envelope, \u00a72 host chokepoint) plus a how-to
#: section that is not an invariant; a floor above the real count makes the gate
#: permanently red, which `.github/workflows/contract-gate.yml` calls worse than
#: no gate. Raise this to 6 in the same change that adds four invariants.
MIN_INVARIANTS = 2
MIN_CONTRACTS = 1

#: A numbered invariant heading: `## 1. The container permission envelope`.
_INVARIANT_HEADING = re.compile(r"^## (\d+)\.\s+(\S.*)$")

#: A contract heading in the other ledger: `## Contract 2: The trust boundary ...`.
_CONTRACT_HEADING = re.compile(r"^## Contract (\d+):\s+(\S.*)$")

#: The parity line itself, matched anywhere in an invariant's body.
_CONTRACT_FIELD = re.compile(r"^\*\*Contract:\*\*\s*(.*)$")

#: Any level-2 heading: the boundary of an invariant's body.
_H2 = re.compile(r"^## ")

#: `Contract 1`, `Contract 1:`, `docs/CONTRACTS.md \u00a71`, `docs/CONTRACTS.md, \u00a71`.
_REFERENCE = re.compile(
    r"^(?:docs/CONTRACTS\.md\s*,?\s*)?"
    r"(?:\u00a7\s*(?P<section>[0-9]+)|Contract\s+(?P<contract>[0-9]+)\s*:?)$"
)

#: `GAP <dash> <reason>`, with em dash, en dash or plain hyphen.
_GAP = re.compile(r"^GAP\s*[\u2014\u2013-]\s*(?P<reason>.+)$")

#: A gap reason shorter than this is not a statement, it is a shrug.
_GAP_MIN_REASON = 8


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def invariant_sections() -> list[dict]:
    """Every `## N. <title>` section: its heading, its line and its body lines.

    The body stops at the next level-2 heading, so a `**Contract:**` line that
    belongs to the next section cannot be credited to this one.
    """
    lines = _read(INVARIANT_LEDGER)
    sections = []
    for index, line in enumerate(lines):
        match = _INVARIANT_HEADING.match(line)
        if not match:
            continue
        end = len(lines)
        for later in range(index + 1, len(lines)):
            if _H2.match(lines[later]):
                end = later
                break
        sections.append(
            {
                "line": index + 1,
                "number": int(match.group(1)),
                "title": match.group(2),
                "heading": line,
                "body": lines[index + 1 : end],
            }
        )
    return sections


def contract_headings() -> dict:
    """`{number: title}` for every `## Contract N: <title>` heading."""
    found: dict = {}
    for line in _read(CONTRACT_LEDGER):
        match = _CONTRACT_HEADING.match(line)
        if match:
            found.setdefault(int(match.group(1)), match.group(2))
    return found


def contract_fields(body: list[str]) -> list[str]:
    """The `**Contract:**` values declared in one invariant's body."""
    values = []
    for line in body:
        match = _CONTRACT_FIELD.match(line)
        if match:
            values.append(match.group(1).strip())
    return values


def value_offence(value: str, contracts: dict) -> str:
    """Why `value` is not an acceptable `**Contract:**` value; '' when it is one."""
    text = value.strip()

    reference = _REFERENCE.match(text)
    if reference:
        number = int(reference.group("section") or reference.group("contract"))
        if number in contracts:
            return ""
        known = ", ".join(f"Contract {n}" for n in sorted(contracts)) or (
            f"no `## Contract N:` heading at all"
        )
        return (
            f"names Contract {number}, which {CONTRACTS_REL} does not define "
            f"(defined: {known})"
        )

    gap = _GAP.match(text)
    if gap:
        reason = gap.group("reason").strip()
        if len(reason) >= _GAP_MIN_REASON:
            return ""
        return (
            f"is a GAP whose reason is {len(reason)} character(s) ({reason!r}); "
            f"a named gap needs at least {_GAP_MIN_REASON} -- an unnamed gap is "
            f"the same as no line at all"
        )

    if not text:
        return "is empty"
    return (
        f"is neither a reference to a `## Contract N:` heading (`Contract 2`, "
        f"`Contract 2:`, `{CONTRACTS_REL} \u00a72`, `{CONTRACTS_REL}, \u00a72`) "
        f"nor `GAP \u2014 <reason>`"
    )


def test_the_two_ledgers_exist():
    assert INVARIANT_LEDGER.is_file(), f"[rule (c)] the invariant ledger is missing: {INVARIANT_LEDGER}"
    assert CONTRACT_LEDGER.is_file(), f"[rule (c)] the contract ledger is missing: {CONTRACT_LEDGER}"


def test_the_ledgers_are_not_vacuous():
    """Rule (c): a parity check over zero headings passes for the wrong reason."""
    sections = invariant_sections()
    contracts = contract_headings()

    assert len(sections) >= MIN_INVARIANTS, (
        f"[rule (c)] {INVARIANTS_REL}: found {len(sections)} numbered invariant "
        f"heading(s) ({', '.join(s['heading'] for s in sections) or 'none'}), but the "
        f"floor is {MIN_INVARIANTS}. A numeral-prefixed `## N. <title>` heading is the "
        f"only thing this file checks, so a truncated or re-formatted ledger would "
        f"otherwise pass silently."
    )
    assert len(contracts) >= MIN_CONTRACTS, (
        f"[rule (c)] {CONTRACTS_REL}: found {len(contracts)} `## Contract N: <title>` "
        f"heading(s), but the floor is {MIN_CONTRACTS}. With none, every invariant "
        f"could only ever be a GAP and the reference half of the check would be dead."
    )


def test_every_numbered_invariant_carries_one_contract_line():
    """Rule (a): one `**Contract:** ...` line per invariant, before the next `## `."""
    offenders = []
    for section in invariant_sections():
        values = contract_fields(section["body"])
        if len(values) == 1:
            continue
        where = f"{INVARIANTS_REL}:{section['line']} {section['heading']!r}"
        if not values:
            offenders.append(
                f"[rule (a)] {where} -- no `**Contract:** ...` line between this heading "
                f"and the next `## ` heading; the section names no contract and no gap"
            )
        else:
            offenders.append(
                f"[rule (a)] {where} -- {len(values)} `**Contract:**` line(s) "
                f"{values!r}; exactly one is required, because two answers are not an "
                f"answer"
            )

    assert not offenders, (
        "every numbered invariant must state where its contract is:\n  "
        + "\n  ".join(offenders)
    )


def test_every_contract_value_resolves_or_names_a_gap():
    """Rule (b): the value is a real `## Contract N:` reference or a reasoned GAP."""
    contracts = contract_headings()
    offenders = []
    for section in invariant_sections():
        for value in contract_fields(section["body"]):
            offence = value_offence(value, contracts)
            if not offence:
                continue
            where = f"{INVARIANTS_REL}:{section['line']} {section['heading']!r}"
            offenders.append(f"[rule (b)] {where} -- `**Contract:** {value}` {offence}")

    assert not offenders, (
        "every `**Contract:**` value must resolve to a real contract heading or name "
        "a gap with a reason:\n  " + "\n  ".join(offenders)
    )


def test_the_value_grammar_is_what_the_docstring_documents():
    """Rule (b) has two branches; the live ledger exercises one of them.

    Both are pinned here so the reference branch cannot rot unnoticed while every
    invariant happens to be a GAP.
    """
    contracts = contract_headings()
    for good in (
        "Contract 1",
        "Contract 1:",
        f"{CONTRACTS_REL} \u00a71",
        f"{CONTRACTS_REL}, \u00a71",
        "GAP \u2014 no heading in docs/CONTRACTS.md names this rule",
        "GAP - no heading in docs/CONTRACTS.md names this rule",
        "GAP \u2013 no heading in docs/CONTRACTS.md names this rule",
    ):
        assert value_offence(good, contracts) == "", good

    unknown = max(contracts) + 1 if contracts else 1
    for bad in (
        "",
        "Contract",
        f"Contract {unknown}",
        f"Contract {unknown}:",
        f"{CONTRACTS_REL} \u00a7{unknown}",
        "GAP",
        "GAP \u2014 short",
        "GAP \u2014    ",
        "see above",
        "Contract 1 and Contract 2",
    ):
        assert value_offence(bad, contracts) != "", bad
