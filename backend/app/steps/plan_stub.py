"""Richer offline plan generator for AGENT_PLAN_STUB.

Deterministic (same ticket + repo state -> same plan; NO LLM call, zero
tokens) but varied across tickets: the inferred ticket type, referenced
files, and narrative differ based on the ticket content and the real repo
map, so demos and pipeline tests see plausible, ticket-shaped plans instead
of one static placeholder.

Every stub plan carries an unmistakable marker (summary prefix + a mandatory
first risk entry) so it can never be mistaken for a real, model-generated
plan — richer output must not compromise that guarantee.
"""

from __future__ import annotations

import random
import re
from pathlib import PurePosixPath

from app.schemas.plan import ChangeAction, ImpactedFile, Plan, ProposedChange, TicketType
from app.schemas.repomap import RepoMap
from app.schemas.ticket import TicketData
from app.steps.repo_digest import parse_repo_map_files

_STUB_MARKER = "[STUB PLAN — no LLM call]"
_MANDATORY_RISK = (
    "This is a stub plan generated without a model call (AGENT_PLAN_STUB=true); "
    "it has not analyzed the actual code and must not be used for real "
    "implementation decisions."
)
_MAX_FILES = 3
_MAX_EXTRA_RISKS = 2

_TYPE_KEYWORDS: dict[TicketType, tuple[str, ...]] = {
    TicketType.BUG: (
        "bug",
        "fix",
        "error",
        "crash",
        "broken",
        "exception",
        "defect",
        "regression",
        "fail",
        "failing",
    ),
    TicketType.FEATURE: (
        "add",
        "implement",
        "feature",
        "support",
        "introduce",
        "enable",
        "allow",
        "new",
    ),
    TicketType.REFACTOR: (
        "refactor",
        "cleanup",
        "restructure",
        "simplify",
        "rename",
        "extract",
        "reorganize",
        "consolidate",
    ),
    TicketType.CHORE: (
        "chore",
        "upgrade",
        "bump",
        "dependency",
        "dependencies",
        "config",
        "tooling",
        "documentation",
    ),
}

_TEST_STRATEGY_BY_TYPE: dict[TicketType, tuple[str, ...]] = {
    TicketType.BUG: (
        "Add a regression test that reproduces the reported issue before the fix "
        "and passes after it.",
        "Write a failing test capturing the bug, confirm it passes once fixed, "
        "and re-run the existing suite for regressions.",
    ),
    TicketType.FEATURE: (
        "Add unit tests covering the new behavior, plus an integration test "
        "exercising the end-to-end flow.",
        "Cover the new code path with unit tests and a regression test for the "
        "ticket's primary use case.",
    ),
    TicketType.REFACTOR: (
        "Rely on the existing test suite to confirm behavior is unchanged; add "
        "characterization tests first if coverage is thin.",
        "Run the full existing suite before and after to confirm no behavioral "
        "change; add tests for any previously uncovered paths touched.",
    ),
    TicketType.CHORE: (
        "Run the existing test suite and relevant lint/type checks to confirm the change is safe.",
        "Verify the build and existing tests still pass; no new coverage "
        "expected for a pure config/dependency change.",
    ),
    TicketType.UNKNOWN: (
        "Add tests appropriate to the change once the scope is clarified; run "
        "the existing suite to check for regressions.",
    ),
}

_RISK_POOL: tuple[str, ...] = (
    "The ticket description may omit edge cases that only surface during implementation.",
    "Changes may have ripple effects on code that depends on the modified files.",
    "Existing test coverage on the modified paths is unverified in stub mode.",
    "Third-party integrations referenced by these files are not checked in stub mode.",
    "The change may touch shared/core modules used elsewhere in the codebase.",
)

_OPEN_QUESTION_POOL: tuple[str, ...] = (
    "Are there existing tests or docs for this area not surfaced by the repo map?",
    "Does this ticket require changes to any public API contract or database schema?",
    "Should this be coordinated with other in-flight tickets touching the same files?",
    "Is a staged rollout or feature flag appropriate for this change?",
)
_OPEN_QUESTION_COUNT_WEIGHTS = (0, 1, 1, 2)  # weighted toward exactly one


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def _infer_ticket_type(ticket_tokens: set[str]) -> TicketType:
    best_type = TicketType.UNKNOWN
    best_score = 0
    for ticket_type, keywords in _TYPE_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in ticket_tokens)
        if score > best_score:
            best_score = score
            best_type = ticket_type
    return best_type


def _path_tokens(path: str) -> set[str]:
    stem = PurePosixPath(path).stem
    return _tokenize(re.sub(r"[/_.\-]", " ", stem))


def _rank_files(files: list[tuple[str, int]], ticket_tokens: set[str]) -> list[str]:
    """Rank candidate files: keyword relevance to the ticket first, then
    symbol count (a proxy for "this is a core module"), most relevant first."""

    def score(item: tuple[str, int]) -> tuple[int, int]:
        path, symbol_count = item
        overlap = len(_path_tokens(path) & ticket_tokens)
        return (overlap, symbol_count)

    return [path for path, _ in sorted(files, key=score, reverse=True)]


def _slug(ticket_key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", ticket_key.lower()).strip("_") or "change"


def _change_description(ticket_type: TicketType, file_path: str, *, is_new: bool) -> str:
    if ticket_type == TicketType.BUG:
        return f"Investigate and fix the reported issue in `{file_path}`."
    if ticket_type == TicketType.FEATURE:
        if is_new:
            return f"Create `{file_path}` to house the new functionality described in the ticket."
        return f"Extend `{file_path}` to support the behavior described in the ticket."
    if ticket_type == TicketType.REFACTOR:
        return (
            f"Restructure `{file_path}` for clarity/maintainability without "
            "changing external behavior."
        )
    if ticket_type == TicketType.CHORE:
        return f"Update `{file_path}` to apply the requested configuration or dependency change."
    return f"Review `{file_path}` and make the changes implied by the ticket description."


def _impacted_reason(symbol_count: int, *, matched: bool) -> str:
    if matched:
        return "Path/name overlaps with keywords from the ticket."
    if symbol_count > 0:
        return f"Core module in the repo map ({symbol_count} symbols) — a plausible starting point."
    return "Present in the repo map; relevance not otherwise determined in stub mode."


def _select_changes(
    ticket_type: TicketType,
    ticket_key: str,
    ranked_paths: list[str],
    by_path: dict[str, int],
    ticket_tokens: set[str],
) -> tuple[list[ImpactedFile], list[ProposedChange]]:
    selected = ranked_paths[:_MAX_FILES]
    if not selected:
        return [], [
            ProposedChange(
                file="TBD",
                action=ChangeAction.MODIFY,
                description=(
                    "The repo map contained no files to reference; stub mode could not "
                    "identify candidate files for this ticket."
                ),
            )
        ]

    impacted_files: list[ImpactedFile] = []
    proposed_changes: list[ProposedChange] = []
    for path in selected:
        matched = bool(_path_tokens(path) & ticket_tokens)
        impacted_files.append(
            ImpactedFile(path=path, reason=_impacted_reason(by_path.get(path, 0), matched=matched))
        )
        proposed_changes.append(
            ProposedChange(
                file=path,
                action=ChangeAction.MODIFY,
                description=_change_description(ticket_type, path, is_new=False),
            )
        )

    if ticket_type == TicketType.FEATURE:
        # Feature tickets commonly add a new file alongside an existing one —
        # a small extra bit of realism for demos.
        sibling = PurePosixPath(selected[0])
        new_path = str(sibling.with_name(_slug(ticket_key) + sibling.suffix))
        impacted_files.append(
            ImpactedFile(
                path=new_path,
                reason="New module suggested for the feature, alongside a related file.",
            )
        )
        proposed_changes.append(
            ProposedChange(
                file=new_path,
                action=ChangeAction.CREATE,
                description=_change_description(ticket_type, new_path, is_new=True),
            )
        )
    return impacted_files, proposed_changes


def build_stub_plan(
    ticket: TicketData, repo_map: RepoMap, planning_notes: str | None = None
) -> Plan:
    """A schema-valid, ticket-shaped plan produced without any API call — for
    zero-credit pipeline/demo testing (AGENT_PLAN_STUB=true).

    Deterministic per (ticket key + summary + repo map + planning notes):
    re-running the same ticket against the same repo state and notes always
    yields the same stub plan, so it is safe to use in tests and reproducible
    demos.
    """
    rng = random.Random(f"{ticket.key}:{ticket.summary}:{repo_map.text}:{planning_notes or ''}")
    ticket_tokens = _tokenize(f"{ticket.summary} {ticket.description}")
    ticket_type = _infer_ticket_type(ticket_tokens)

    files = parse_repo_map_files(repo_map.text)
    by_path = dict(files)
    ranked_paths = _rank_files(files, ticket_tokens)

    impacted_files, proposed_changes = _select_changes(
        ticket_type, ticket.key, ranked_paths, by_path, ticket_tokens
    )

    test_strategy = rng.choice(_TEST_STRATEGY_BY_TYPE[ticket_type])

    risks = [_MANDATORY_RISK]
    if planning_notes:
        risks.append(
            "Technical notes were supplied alongside this ticket; stub mode does "
            "not analyze them — a real planning run would incorporate them into "
            "the impacted files, proposed changes, and test strategy."
        )
    risks += rng.sample(_RISK_POOL, k=min(_MAX_EXTRA_RISKS, len(_RISK_POOL)))

    open_question_count = rng.choice(_OPEN_QUESTION_COUNT_WEIGHTS)
    open_questions = rng.sample(
        _OPEN_QUESTION_POOL, k=min(open_question_count, len(_OPEN_QUESTION_POOL))
    )

    summary = ticket.summary.strip() or ticket.key
    return Plan(
        summary=f"{_STUB_MARKER} ({ticket_type.value}) {summary}",
        ticket_type=ticket_type,
        impacted_files=impacted_files,
        proposed_changes=proposed_changes,
        test_strategy=test_strategy,
        risks=risks,
        open_questions=open_questions,
    )
