"""Mapping Guesty task rows and task objects to the canonical task."""

from datetime import UTC, datetime

import pytest
from guesty_task_helpers import (
    CANCELED_ID,
    CLEANING_ID,
    CREATED_ID,
    INJECTION_TEXT,
    INSPECTION_ID,
    LISTING_ID,
    MAINTENANCE_ID,
    NEVER_SHOWN,
    RESERVATION_ID,
    fixture,
)

from pmsbridge.pms.guesty.tasks import map_task
from pmsbridge.pms.models import ChecklistItem, TaskStatus, TaskType


def rows() -> list[object]:
    found = fixture("list_page.json")
    assert isinstance(found, list)
    return found


def test_a_cleaning_row_is_mapped_from_the_wrapped_values() -> None:
    task = map_task(rows()[0])
    assert task is not None
    assert task.id == CLEANING_ID and task.title == "Turnover clean"
    assert task.task_type is TaskType.CLEANING and task.status is TaskStatus.OPEN
    assert task.listing_id == LISTING_ID and task.reservation_id == RESERVATION_ID
    assert task.due_at == datetime(2026, 12, 1, 10, 0, tzinfo=UTC)
    assert task.assignee_name is None
    assert task.extras["assignee_id"] == "6a8000000000000000000u01"
    assert task.extras["status_detail"] == "pending"


def test_the_other_rows_map_their_kind_and_state() -> None:
    maintenance, inspection, canceled = (map_task(rows()[n]) for n in (1, 2, 3))
    assert maintenance is not None and inspection is not None and canceled is not None
    assert maintenance.id == MAINTENANCE_ID and maintenance.status is TaskStatus.IN_PROGRESS
    assert maintenance.task_type is TaskType.MAINTENANCE and maintenance.reservation_id is None
    assert inspection.id == INSPECTION_ID and inspection.status is TaskStatus.DONE
    assert inspection.task_type is TaskType.INSPECTION
    assert inspection.extras["completed_at"] == "2026-11-19T15:30:00.000Z"
    assert canceled.id == CANCELED_ID and canceled.status is TaskStatus.CANCELLED
    assert canceled.task_type is TaskType.OTHER and canceled.extras["type_detail"] == "runner"
    assert canceled.title == INJECTION_TEXT  # passed on as data, never acted on
    assert canceled.due_at is None and canceled.listing_id is None


def test_a_row_without_an_id_is_skipped() -> None:
    assert map_task(rows()[4]) is None
    assert map_task("text") is None and map_task({"_id": "  "}) is None


def test_a_task_object_maps_the_checklist_listing_and_due_time() -> None:
    task = map_task(fixture("create_response.json"))
    assert task is not None and task.id == CREATED_ID
    assert task.title == "Deep clean before arrival"
    assert task.description == "Focus on the kitchen and the bathrooms."
    assert task.task_type is TaskType.CLEANING and task.status is TaskStatus.OPEN
    assert task.listing_id == LISTING_ID and task.reservation_id == RESERVATION_ID
    assert task.due_at == datetime(2026, 12, 1, 10, 0, tzinfo=UTC)
    assert task.checklist == [
        ChecklistItem(text="Wash the linen", done=True),
        ChecklistItem(text="Restock soap", done=False),
        ChecklistItem(text="Check the oven", done=False),
    ]
    assert task.extras["priority"] == "medium"
    for hidden in NEVER_SHOWN:
        assert hidden not in str(task)


def test_plain_checklist_strings_use_the_finished_list() -> None:
    task = map_task(
        {
            "_id": "a",
            "title": "T",
            "checklist": ["One", {"name": "Two"}, 7, ""],
            "checklistFinished": ["Two", {"name": "Ghost"}],
        }
    )
    assert task is not None
    assert task.checklist == [
        ChecklistItem(text="One", done=False),
        ChecklistItem(text="Two", done=True),
    ]


def test_the_checklist_is_capped() -> None:
    task = map_task({"_id": "a", "checklist": [f"item {n}" for n in range(200)]})
    assert task is not None and len(task.checklist) == 50


def test_a_fixed_time_task_is_due_at_its_start_and_a_missing_title_is_unknown() -> None:
    task = map_task(
        {"_id": "a", "timing": {"type": "specific", "startTime": "2026-12-05T09:00:00Z"}}
    )
    assert task is not None and task.title == "Unknown"
    assert task.due_at == datetime(2026, 12, 5, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("pending", TaskStatus.OPEN),
        ("Confirmed", TaskStatus.OPEN),
        ("in progress", TaskStatus.IN_PROGRESS),
        ("IN_PROGRESS", TaskStatus.IN_PROGRESS),
        ("completed", TaskStatus.DONE),
        ("canceled", TaskStatus.CANCELLED),
        ("cancelled", TaskStatus.CANCELLED),
        ("something new", TaskStatus.UNKNOWN),
        (None, TaskStatus.UNKNOWN),
        (5, TaskStatus.UNKNOWN),
    ],
)
def test_every_state_maps(status: object, expected: TaskStatus) -> None:
    task = map_task({"_id": "a", "status": status})
    assert task is not None and task.status is expected


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("cleaning", TaskType.CLEANING),
        ("Check-in", TaskType.CHECK_IN),
        ("check in", TaskType.CHECK_IN),
        ("sanitation", TaskType.OTHER),
        ("mid-stay", TaskType.OTHER),
        ("invented", TaskType.UNKNOWN),
        (None, TaskType.UNKNOWN),
    ],
)
def test_every_kind_maps(kind: object, expected: TaskType) -> None:
    task = map_task({"_id": "a", "type": kind})
    assert task is not None and task.task_type is expected


def test_values_of_the_wrong_type_are_ignored_instead_of_failing() -> None:
    task = map_task(
        {
            "_id": "a",
            "title": 5,
            "description": ["x"],
            "apply": "x",
            "listing": 3,
            "reservation": 9,
            "timing": "soon",
            "scheduledFor": [],
            "assignee": "me",
            "checklist": "text",
            "checklistAggregated": {"a": 1},
        }
    )
    assert task is not None and task.title == "Unknown" and task.checklist == []
    assert task.listing_id is None and task.reservation_id is None and task.due_at is None


def test_a_reservation_given_as_plain_text_is_read_and_a_date_without_an_offset_is_not() -> None:
    task = map_task(
        {"_id": "a", "reservation": "R9", "timing": {"mustFinishBefore": "2026-12-01T10:00:00"}}
    )
    assert task is not None and task.reservation_id == "R9" and task.due_at is None


def test_a_wrapped_answer_is_unwrapped() -> None:
    task = map_task({"data": {"_id": "a", "title": "Wrapped"}})
    assert task is not None and task.id == "a" and task.title == "Wrapped"
