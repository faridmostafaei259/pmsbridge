"""Guesty tasks (including cleaning tasks): list them and create one.

Endpoints (see docs/providers/guesty/tasks.md): `GET /tasks-open-api/tasks` and
`POST /tasks-open-api/create-single-task`. Task titles, descriptions and checklist items are written
by people and are untrusted: they are only ever passed on inside the canonical `Task` and are never
logged. Only the id of the person a task is assigned to is read (as a display extra): Guesty's task
payloads carry no name, and no contact detail is ever read.

Guesty documents how to filter a task list only for the status (`@nin` on `status`) and for
today's tasks. So the status is filtered by Guesty, and the listing, type and due-date filters are
applied here, on the rows Guesty returns, with a bounded scan. Every filter is also checked here,
so a filter that Guesty ignored cannot return a wrong task.

Creating a task is one POST and is never sent again (a 429 with Retry-After is the only exception,
handled by the shared client). On a timeout the error says the outcome is unknown. Shared helpers
are in `common.py`.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from pydantic import ValidationError

from pmsbridge.pms.base import (
    Capability,
    PMSUpstreamError,
    PMSValidationError,
    remaining_seconds,
)
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import (
    MAX_SKIP,
    as_dict,
    as_list,
    datetime_of,
    decode_cursor,
    encode_cursor,
    flag_of,
    text_of,
)
from pmsbridge.pms.models import (
    MAX_CHECKLIST_ITEMS,
    MAX_PAGE_SIZE,
    UNKNOWN_NAME,
    ChecklistItem,
    ExtraValue,
    NewTask,
    Page,
    Task,
    TaskQuery,
    TaskStatus,
    TaskType,
)

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.TASKS_READ, Capability.TASKS_CREATE})

TASKS_PATH: Final = "/tasks-open-api/tasks"
CREATE_PATH: Final = "/tasks-open-api/create-single-task"
# `columns` is required by Guesty. The names are those in Guesty's example, plus `type`.
COLUMNS: Final = (
    "status taskTitle type listing reservation scheduledFor endTime canStartAfter "
    "mustFinishBefore assignee id"
)
MIN_SECONDS_PER_WRITE: Final = 2.0
# Guesty states no default or maximum page size. At most this many pages are read for one call.
MAX_SCAN_PAGES: Final = 10

# Every status Guesty documents, in its own words.
_GUESTY_STATUSES: Final = ("pending", "confirmed", "in progress", "completed", "canceled")
_STATUS_OF: Final[dict[str, TaskStatus]] = {
    "pending": TaskStatus.OPEN,
    "confirmed": TaskStatus.OPEN,
    "in progress": TaskStatus.IN_PROGRESS,
    "completed": TaskStatus.DONE,
    "canceled": TaskStatus.CANCELLED,
    "cancelled": TaskStatus.CANCELLED,
}
_TYPE_OF: Final[dict[str, TaskType]] = {
    "cleaning": TaskType.CLEANING,
    "maintenance": TaskType.MAINTENANCE,
    "inspection": TaskType.INSPECTION,
    "check-in": TaskType.CHECK_IN,
    # The other types Guesty documents have no canonical equivalent.
    "mid-stay": TaskType.OTHER,
    "runner": TaskType.OTHER,
    "sanitation": TaskType.OTHER,
    "issue": TaskType.OTHER,
    "inventory": TaskType.OTHER,
    "administration": TaskType.OTHER,
}
# The canonical types Guesty can store. Other is left out of the request: Guesty then uses its own.
_GUESTY_TYPE: Final[dict[TaskType, str]] = {
    TaskType.CLEANING: "cleaning",
    TaskType.MAINTENANCE: "maintenance",
    TaskType.INSPECTION: "inspection",
    TaskType.CHECK_IN: "check-in",
}


# ---- Mapping ----


def _scalars(values: Mapping[str, ExtraValue]) -> dict[str, ExtraValue]:
    return {key: value for key, value in values.items() if value is not None}


def _label(value: object) -> str | None:
    """A text value that a list row may wrap as `{children: text}`."""
    wrapped = as_dict(value)
    return text_of(wrapped.get("children")) if wrapped is not None else text_of(value)


def _status_key(value: object) -> str | None:
    text = _label(value)
    return " ".join(text.lower().replace("_", " ").split()) if text is not None else None


def _type_key(value: object) -> str | None:
    text = _label(value)
    return "-".join(text.lower().replace("_", " ").split()) if text is not None else None


def _status_of(value: object) -> TaskStatus:
    key = _status_key(value)
    return _STATUS_OF.get(key, TaskStatus.UNKNOWN) if key is not None else TaskStatus.UNKNOWN


def _type_of(value: object) -> TaskType:
    key = _type_key(value)
    return _TYPE_OF.get(key, TaskType.UNKNOWN) if key is not None else TaskType.UNKNOWN


def _entry_name(entry: object) -> str | None:
    named = as_dict(entry)
    return text_of(named.get("name")) if named is not None else text_of(entry)


def _checklist(item: Mapping[str, object]) -> list[ChecklistItem]:
    """The checklist: Guesty's aggregated entries when it gives them, else the plain strings.

    The person who ticked an item (`by`) is never read.
    """
    aggregated = as_list(item.get("checklistAggregated")) or []
    found: list[ChecklistItem] = []
    for entry in aggregated[:MAX_CHECKLIST_ITEMS]:
        row = as_dict(entry)
        name = _entry_name(entry)
        if name is not None:
            done = flag_of(row.get("finished")) is True if row is not None else False
            found.append(ChecklistItem(text=name, done=done))
    if found:
        return found
    finished = {
        name
        for entry in as_list(item.get("checklistFinished")) or []
        if (name := _entry_name(entry))
    }
    for entry in (as_list(item.get("checklist")) or [])[:MAX_CHECKLIST_ITEMS]:
        name = _entry_name(entry)
        if name is not None:
            found.append(ChecklistItem(text=name, done=name in finished))
    return found


def _first_datetime(*values: object) -> datetime | None:
    for value in values:
        if (parsed := datetime_of(value)) is not None:
            return parsed
    return None


def _due_at(item: Mapping[str, object]) -> datetime | None:
    """When the task must be finished, else when a task with a fixed time starts."""
    timing = as_dict(item.get("timing")) or {}
    scheduled = as_dict(item.get("scheduledFor")) or {}
    return _first_datetime(
        timing.get("mustFinishBefore"),
        scheduled.get("mustFinishBefore"),
        item.get("mustFinishBefore"),
        timing.get("startTime"),
        scheduled.get("startTime"),
    )


def _listing_id(item: Mapping[str, object]) -> str | None:
    apply = as_dict(item.get("apply")) or {}
    listing = as_dict(item.get("listing")) or {}
    return text_of(apply.get("listingId")) or text_of(listing.get("listingId"))


def _reservation_id(item: Mapping[str, object]) -> str | None:
    apply = as_dict(item.get("apply")) or {}
    reservation = item.get("reservation")
    inner = as_dict(reservation)
    return (
        text_of(apply.get("reservationId"))
        or (text_of(inner.get("reservationId")) if inner is not None else None)
        or (text_of(reservation) if inner is None else None)
    )


def _assignee_id(item: Mapping[str, object]) -> str | None:
    assignee = as_dict(item.get("assignee")) or {}
    return text_of(item.get("assigneeId")) or text_of(assignee.get("assigneeId"))


def _completed_at(item: Mapping[str, object]) -> str | None:
    end = item.get("endTime")
    wrapped = as_dict(end)
    return text_of(wrapped.get("date")) if wrapped is not None else text_of(end)


def _item_of(raw: object) -> dict[str, object] | None:
    """The task object itself: some answers wrap it in `data`."""
    item = as_dict(raw)
    if item is None:
        return None
    if "_id" not in item and "id" not in item and (inner := as_dict(item.get("data"))) is not None:
        return inner
    return item


def map_task(raw: object) -> Task | None:
    """The canonical task, or None (logged without any detail) for an unusable item.

    Reads both shapes Guesty documents: a list row (`id`, `taskTitle`, wrapped values) and a task
    object (`_id`, `title`).
    """
    item = _item_of(raw)
    task_id = (text_of(item.get("_id")) or text_of(item.get("id"))) if item is not None else None
    if item is None or task_id is None:
        logger.warning("task skipped: no usable id")
        return None
    status_raw = _status_key(item.get("status"))
    type_raw = _type_key(item.get("type"))
    extras = _scalars(
        {
            "status_detail": status_raw,
            "type_detail": type_raw if _type_of(item.get("type")) is TaskType.OTHER else None,
            "priority": text_of(item.get("priority")),
            "assignee_id": _assignee_id(item),
            "completed_at": _completed_at(item),
        }
    )
    try:
        return Task(
            id=task_id,
            title=_label(item.get("title")) or _label(item.get("taskTitle")) or UNKNOWN_NAME,
            task_type=_type_of(item.get("type")),
            checklist=_checklist(item),
            description=text_of(item.get("description")),
            listing_id=_listing_id(item),
            reservation_id=_reservation_id(item),
            status=_status_of(item.get("status")),
            due_at=_due_at(item),
            assignee_name=None,  # Guesty's task payloads carry no name, only the person's id.
            extras=extras,
        )
    except ValidationError:
        # The error text would repeat the task's words, so only the id is logged.
        logger.warning("task skipped: could not be mapped", extra={"task_id": task_id[:80]})
        return None


def _rows_of(body: object) -> list[object]:
    """The rows of a list answer. Guesty documents a plain array; `data` and `results` also work."""
    found = as_list(body)
    envelope = as_dict(body)
    if found is None and envelope is not None:
        found = as_list(envelope.get("results"))
        if found is None:
            data = envelope.get("data")
            found = as_list(data)
            if found is None and (inner := as_dict(data)) is not None:
                found = as_list(inner.get("results"))
    if found is None:
        raise PMSUpstreamError("Guesty returned a task list that could not be understood.")
    if len(found) > MAX_PAGE_SIZE:
        raise PMSUpstreamError("Guesty returned more tasks than expected.")
    return found


# ---- Filters ----


def _status_filter(wanted: TaskStatus) -> str:
    """Guesty's filter string for one status: every other documented status is excluded.

    Written like Guesty's example (single quotes), using only the documented `@nin` operator.
    """
    excluded = [name for name in _GUESTY_STATUSES if _STATUS_OF[name] is not wanted]
    listed = ",".join(f"'{name}'" for name in excluded)
    return "{'status':{'@nin':[" + listed + "]}}"


def _check_query(query: TaskQuery) -> None:
    if query.status is TaskStatus.UNKNOWN:
        raise PMSValidationError("A task list cannot be filtered by an unknown status.")
    if query.task_type is TaskType.UNKNOWN:
        raise PMSValidationError("A task list cannot be filtered by an unknown type.")


def _matches(task: Task, query: TaskQuery) -> bool:
    if query.listing_id is not None and task.listing_id != query.listing_id:
        return False
    if query.status is not None and task.status is not query.status:
        return False
    if query.task_type is not None and task.task_type is not query.task_type:
        return False
    return _due_matches(task, query)


def _due_matches(task: Task, query: TaskQuery) -> bool:
    if query.due_from is None and query.due_to is None:
        return True
    if task.due_at is None:
        return False
    due = task.due_at.astimezone(UTC).date()
    return (query.due_from is None or due >= query.due_from) and (
        query.due_to is None or due <= query.due_to
    )


def _filtered_here(query: TaskQuery) -> bool:
    return (
        query.listing_id is not None
        or query.task_type is not None
        or query.due_from is not None
        or query.due_to is not None
    )


# ---- Creating ----


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def create_body(task: NewTask, *, now: datetime | None = None) -> dict[str, object]:
    """The request body, or a refusal that sends nothing.

    A due time is sent as a flexible window: the task can start now and must finish before it.
    Times are written in UTC.
    """
    if task.task_type is TaskType.UNKNOWN or task.task_type is TaskType.CHECK_OUT:
        raise PMSValidationError(
            "This task type is not available: use cleaning, maintenance, inspection, check_in "
            "or other."
        )
    body: dict[str, object] = {"title": task.title}
    if task.description is not None:
        body["description"] = task.description
    if task.task_type in _GUESTY_TYPE:
        body["type"] = _GUESTY_TYPE[task.task_type]
    if task.checklist:
        body["checklist"] = list(task.checklist)
    if task.listing_id is not None:
        body["listingId"] = task.listing_id
    if task.reservation_id is not None:
        body["reservationId"] = task.reservation_id
    if task.due_at is not None:
        start = now or datetime.now(UTC)
        if task.due_at <= start:
            raise PMSValidationError("The due time is in the past, so nothing was created.")
        body["canStartAfter"] = _utc_text(start)
        body["mustFinishBefore"] = _utc_text(task.due_at)
    return body


# ---- Adapter ----


class GuestyTasks:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def list_tasks(self, query: TaskQuery) -> Page[Task]:
        _check_query(query)
        skip = decode_cursor(query.cursor)
        # With filters applied here, whole pages are read so a page is not mostly rejected rows.
        size = MAX_PAGE_SIZE if _filtered_here(query) else query.limit
        items: list[Task] = []
        ended = False
        for _ in range(MAX_SCAN_PAGES):
            raw = await self._page(query, size, skip)
            if not raw:
                # Guesty states no page size, so only an empty page is known to be the end.
                ended = True
                break
            for row in raw:
                skip += 1
                found = map_task(row)
                if found is not None and _matches(found, query):
                    items.append(found)
                    if len(items) >= query.limit:
                        return Page[Task](items=items, next_cursor=self._cursor(skip, more=True))
        return Page[Task](items=items, next_cursor=self._cursor(skip, more=not ended))

    @staticmethod
    def _cursor(skip: int, *, more: bool) -> str | None:
        return encode_cursor(skip) if more and skip <= MAX_SKIP else None

    async def _page(self, query: TaskQuery, size: int, skip: int) -> list[object]:
        params: dict[str, str | int] = {"columns": COLUMNS, "limit": size, "skip": skip}
        if query.status is not None:
            params["filters"] = _status_filter(query.status)
        response = await self._client.request("GET", TASKS_PATH, params=params)
        return _rows_of(response.json())

    async def create_task(self, task: NewTask) -> Task:
        body = create_body(task)
        left = remaining_seconds()
        if left is not None and left < MIN_SECONDS_PER_WRITE:
            raise PMSUpstreamError("Nothing was created: the time allowed ran out. Try again.")
        response = await self._client.request("POST", CREATE_PATH, json_body=body)
        unclear = PMSUpstreamError(
            "Guesty accepted the task but its answer could not be understood. The task was "
            "probably created: check the task list in Guesty before trying again."
        )
        try:
            answer = response.json()
        except PMSUpstreamError:
            raise unclear from None
        created = map_task(answer)
        if created is None:
            raise unclear
        return created


def build(client: GuestyClient) -> GuestyTasks:
    return GuestyTasks(client)
