"""The plan schema is a versioned contract — these tests pin its behavior."""

import pytest
from pydantic import ValidationError

from app.schemas.export import SCHEMA_PATH, render
from app.schemas.plan import SCHEMA_VERSION, Plan


def valid_plan_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "summary": "Add a --verbose flag to the CLI.",
        "ticket_type": "feature",
        "estimated_story_points": 2,
        "complexity_level": "low",
        "impacted_files": [{"path": "src/cli.py", "reason": "flag parsing lives here"}],
        "proposed_changes": [
            {
                "file": "src/cli.py",
                "action": "modify",
                "description": "Register --verbose and thread it into the logger setup.",
            }
        ],
        "test_strategy": "Unit test flag parsing; snapshot the help output.",
        "risks": ["Existing scripts may parse the old help text."],
        "open_questions": [],
    }


def test_valid_plan_parses() -> None:
    plan = Plan.model_validate(valid_plan_payload())
    assert plan.schema_version == SCHEMA_VERSION
    assert plan.proposed_changes[0].action == "modify"


def test_extra_fields_are_rejected() -> None:
    payload = valid_plan_payload() | {"surprise": "field"}
    with pytest.raises(ValidationError):
        Plan.model_validate(payload)


def test_missing_required_field_is_rejected() -> None:
    payload = valid_plan_payload()
    del payload["summary"]
    with pytest.raises(ValidationError):
        Plan.model_validate(payload)


def test_unknown_schema_version_is_rejected() -> None:
    payload = valid_plan_payload() | {"schema_version": 3}
    with pytest.raises(ValidationError):
        Plan.model_validate(payload)


def test_legacy_schema_version_1_plan_without_estimates_still_parses() -> None:
    """Plans persisted before estimated_story_points/complexity_level existed
    must keep deserializing (as None on those two fields), or every job stored
    under schema_version 1 becomes unreadable — see JobStore.get/list_jobs,
    which round-trip Job (and its nested plan) through model_validate_json."""
    payload = valid_plan_payload() | {"schema_version": 1}
    del payload["estimated_story_points"]
    del payload["complexity_level"]
    plan = Plan.model_validate(payload)
    assert plan.schema_version == 1
    assert plan.estimated_story_points is None
    assert plan.complexity_level is None


def test_empty_proposed_changes_is_rejected() -> None:
    payload = valid_plan_payload() | {"proposed_changes": []}
    with pytest.raises(ValidationError):
        Plan.model_validate(payload)


def test_unknown_ticket_type_is_rejected() -> None:
    payload = valid_plan_payload() | {"ticket_type": "epic"}
    with pytest.raises(ValidationError):
        Plan.model_validate(payload)


def test_unknown_complexity_level_is_rejected() -> None:
    payload = valid_plan_payload() | {"complexity_level": "extreme"}
    with pytest.raises(ValidationError):
        Plan.model_validate(payload)


def test_non_fibonacci_story_points_are_rejected() -> None:
    payload = valid_plan_payload() | {"estimated_story_points": 4}
    with pytest.raises(ValidationError):
        Plan.model_validate(payload)


def test_round_trip() -> None:
    plan = Plan.model_validate(valid_plan_payload())
    assert Plan.model_validate_json(plan.model_dump_json()) == plan


def test_committed_schema_file_matches_model() -> None:
    """schema/plan.schema.json must be regenerated whenever the model changes."""
    assert SCHEMA_PATH.exists(), "run: uv run python -m app.schemas.export"
    assert SCHEMA_PATH.read_text(encoding="utf-8") == render()
