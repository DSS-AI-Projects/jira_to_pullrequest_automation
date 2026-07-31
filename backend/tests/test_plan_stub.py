"""Richer offline stub plan generator (AGENT_PLAN_STUB)."""

from app.schemas.plan import ChangeAction, TicketType
from app.schemas.repomap import RepoMap
from app.schemas.ticket import TicketData
from app.steps.plan_stub import build_stub_plan

REPO_MAP_TEXT = """\
README.md
backend/app/auth/service.py
  class AuthService (line 1)
  fn login (line 10)
  fn logout (line 20)
backend/app/jobs/store.py
  class JobStore (line 1)
  fn create (line 10)
frontend/src/lib/api.ts
  fn fetchRepos (line 1)
frontend/src/components/login-form.tsx
"""


def repo_map() -> RepoMap:
    return RepoMap(text=REPO_MAP_TEXT, file_count=4, symbol_count=6)


def ticket(
    key: str = "PROJ-1", summary: str = "Fix login error", description: str = ""
) -> TicketData:
    return TicketData(key=key, summary=summary, description=description or summary)


# --- safety: the stub must never be mistaken for a real plan ---


def test_summary_carries_stub_marker() -> None:
    plan = build_stub_plan(ticket(), repo_map())
    assert plan.summary.startswith("[STUB PLAN")
    assert "no LLM call" in plan.summary


def test_first_risk_is_the_mandatory_stub_disclosure() -> None:
    plan = build_stub_plan(ticket(), repo_map())
    assert plan.risks[0].startswith("This is a stub plan generated without a model call")


def test_schema_valid_for_every_ticket_type() -> None:
    for summary in (
        "Fix crash on login",
        "Add support for dark mode",
        "Refactor the auth service for clarity",
        "Bump the requests dependency",
        "Investigate slow startup",
    ):
        plan = build_stub_plan(ticket(summary=summary), repo_map())
        assert plan.proposed_changes  # min_length=1 satisfied
        assert plan.test_strategy


# --- ticket-type inference ---


def test_infers_bug_type_from_keywords() -> None:
    plan = build_stub_plan(ticket(summary="Fix crash when logging in"), repo_map())
    assert plan.ticket_type == TicketType.BUG


def test_infers_feature_type_from_keywords() -> None:
    plan = build_stub_plan(ticket(summary="Add support for SSO login"), repo_map())
    assert plan.ticket_type == TicketType.FEATURE


def test_infers_refactor_type_from_keywords() -> None:
    plan = build_stub_plan(ticket(summary="Refactor and simplify the auth service"), repo_map())
    assert plan.ticket_type == TicketType.REFACTOR


def test_infers_chore_type_from_keywords() -> None:
    plan = build_stub_plan(ticket(summary="Bump the auth dependency version"), repo_map())
    assert plan.ticket_type == TicketType.CHORE


def test_unrecognized_ticket_falls_back_to_unknown() -> None:
    plan = build_stub_plan(ticket(summary="Xyzzy plugh", description="Xyzzy plugh"), repo_map())
    assert plan.ticket_type == TicketType.UNKNOWN


# --- file selection references the real repo map ---


def test_modify_changes_target_real_mapped_files() -> None:
    mapped_paths = {
        "README.md",
        "backend/app/auth/service.py",
        "backend/app/jobs/store.py",
        "frontend/src/lib/api.ts",
        "frontend/src/components/login-form.tsx",
    }
    plan = build_stub_plan(ticket(summary="Fix login error"), repo_map())
    modifies = [c for c in plan.proposed_changes if c.action == ChangeAction.MODIFY]
    assert modifies
    assert all(change.file in mapped_paths for change in modifies)


def test_create_change_is_a_sibling_of_a_mapped_file() -> None:
    plan = build_stub_plan(ticket(summary="Add SSO login support"), repo_map())
    creates = [c for c in plan.proposed_changes if c.action == ChangeAction.CREATE]
    modifies = {c.file for c in plan.proposed_changes if c.action == ChangeAction.MODIFY}
    assert creates
    created_dir = creates[0].file.rsplit("/", 1)[0]
    assert any(modified.startswith(created_dir + "/") for modified in modifies)


def test_keyword_matching_file_is_ranked_first() -> None:
    # "store" appears only in backend/app/jobs/store.py's own filename — ranking
    # is by path-token overlap first, so it must be the top pick.
    plan = build_stub_plan(
        ticket(summary="Fix store persistence bug", description="The store loses data."),
        repo_map(),
    )
    changed_files = [change.file for change in plan.proposed_changes]
    assert changed_files[0] == "backend/app/jobs/store.py"


def test_empty_repo_map_falls_back_to_placeholder_file() -> None:
    plan = build_stub_plan(ticket(), RepoMap(text="", file_count=0))
    assert len(plan.proposed_changes) == 1
    assert plan.proposed_changes[0].file == "TBD"


def test_feature_ticket_adds_a_create_change() -> None:
    plan = build_stub_plan(ticket(key="PROJ-9", summary="Add SSO login support"), repo_map())
    creates = [c for c in plan.proposed_changes if c.action == ChangeAction.CREATE]
    assert len(creates) == 1
    assert "proj_9" in creates[0].file.lower()


# --- determinism + variety ---


def test_same_inputs_are_fully_deterministic() -> None:
    first = build_stub_plan(ticket(), repo_map())
    second = build_stub_plan(ticket(), repo_map())
    assert first == second


def test_different_tickets_can_produce_different_narrative() -> None:
    plans = [
        build_stub_plan(ticket(key=f"PROJ-{i}", summary=f"Fix bug number {i}"), repo_map())
        for i in range(8)
    ]
    test_strategies = {plan.test_strategy for plan in plans}
    # multiple distinct test-strategy phrasings should appear across 8 tickets
    assert len(test_strategies) > 1


def test_changing_summary_for_same_key_can_change_output() -> None:
    a = build_stub_plan(ticket(key="PROJ-1", summary="Fix login error"), repo_map())
    b = build_stub_plan(ticket(key="PROJ-1", summary="Add SSO login support"), repo_map())
    assert a.ticket_type != b.ticket_type


# --- planning notes ---


def test_planning_notes_absent_by_default_omits_acknowledgment() -> None:
    plan = build_stub_plan(ticket(), repo_map())
    assert not any("Technical notes were supplied" in risk for risk in plan.risks)


def test_planning_notes_present_adds_acknowledgment_risk() -> None:
    plan = build_stub_plan(ticket(), repo_map(), planning_notes="Use dependency injection.")
    assert any("Technical notes were supplied" in risk for risk in plan.risks)
    assert plan.risks[0].startswith("This is a stub plan generated without a model call")


def test_planning_notes_still_carries_stub_marker_and_mandatory_risk() -> None:
    plan = build_stub_plan(ticket(), repo_map(), planning_notes="Anything at all.")
    assert plan.summary.startswith("[STUB PLAN")
    assert plan.risks[0].startswith("This is a stub plan generated without a model call")


def test_changing_planning_notes_can_change_output_deterministically() -> None:
    a = build_stub_plan(ticket(), repo_map(), planning_notes="Note A")
    b = build_stub_plan(ticket(), repo_map(), planning_notes="Note B")
    a_again = build_stub_plan(ticket(), repo_map(), planning_notes="Note A")
    assert a == a_again  # deterministic for the same notes
    assert a.test_strategy != b.test_strategy or a.risks != b.risks
