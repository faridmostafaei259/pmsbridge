"""The Guesty tasks adapter through the real client, with respx as the only network."""

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta, timezone

import httpx
import pytest
import respx
from guesty_task_helpers import (
    CANCELED_ID,
    CHECKLIST_SECRET,
    CLEANING_ID,
    CREATE_URL,
    CREATED_ID,
    DRAFT_TEXT,
    INJECTION_TEXT,
    INSPECTION_ID,
    LISTING_ID,
    MAINTENANCE_ID,
    RESERVATION_ID,
    TASKS_URL,
    fixture,
    json_response,
    make_client,
    mock_router,
)

from pmsbridge.pms import base
from pmsbridge.pms.base import Capability, PMSUpstreamError, PMSValidationError
from pmsbridge.pms.guesty import tasks
from pmsbridge.pms.guesty.common import encode_cursor
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains
from pmsbridge.pms.models import NewTask, TaskQuery, TaskStatus, TaskType


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        yield mocked


def query_of(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


def api() -> tasks.GuestyTasks:
    return tasks.build(make_client())


def mock_list(router: respx.MockRouter) -> respx.Route:
    """The fixture rows on the first page, then an empty page."""
    return router.get(TASKS_URL).mock(
        side_effect=[json_response(fixture("list_page.json")), json_response([])]
    )


def future(days: int = 30) -> datetime:
    return (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0)


def new_task(**fields: object) -> NewTask:
    return NewTask.model_validate({"title": "Deep clean", **fields})


# ---- list_tasks ----


async def test_list_sends_columns_limit_and_skip(router: respx.MockRouter) -> None:
    route = router.get(TASKS_URL).mock(return_value=json_response([]))
    page = await api().list_tasks(TaskQuery(limit=10))
    assert page.items == [] and page.next_cursor is None
    params = query_of(route)
    assert params == {"columns": tasks.COLUMNS, "limit": "25", "skip": "0"}  # Guesty minimum
    assert "id" in tasks.COLUMNS.split()


async def test_list_maps_the_fixture_and_pages_with_a_skip_cursor(router: respx.MockRouter) -> None:
    route = router.get(TASKS_URL).mock(return_value=json_response(fixture("list_page.json")))
    page = await api().list_tasks(TaskQuery(limit=2))
    assert [t.id for t in page.items] == [CLEANING_ID, MAINTENANCE_ID]
    assert page.next_cursor == encode_cursor(2)
    await api().list_tasks(TaskQuery(limit=2, cursor=page.next_cursor))
    assert query_of(route)["skip"] == "2"


async def test_the_list_ends_on_an_empty_page_and_reads_on_after_a_short_one(
    router: respx.MockRouter,
) -> None:
    route = mock_list(router)
    page = await api().list_tasks(TaskQuery(limit=50))
    # The unusable row is skipped but counted, so the next page starts after it.
    assert [t.id for t in page.items] == [CLEANING_ID, MAINTENANCE_ID, INSPECTION_ID, CANCELED_ID]
    assert page.next_cursor is None and route.call_count == 2
    assert [dict(c.request.url.params)["skip"] for c in route.calls] == ["0", "5"]


async def test_a_page_full_of_rows_gives_a_cursor_even_when_guesty_caps_the_page(
    router: respx.MockRouter,
) -> None:
    router.get(TASKS_URL).mock(return_value=json_response([{"id": "a"}, {"id": "b"}]))
    page = await api().list_tasks(TaskQuery(limit=2))
    assert page.next_cursor == encode_cursor(2)


async def test_the_status_filter_uses_only_the_documented_operator(
    router: respx.MockRouter,
) -> None:
    route = router.get(TASKS_URL).mock(return_value=json_response([]))
    await api().list_tasks(TaskQuery(status=TaskStatus.OPEN))
    assert (
        query_of(route)["filters"] == "{'status':{'@nin':['in progress','completed','canceled']}}"
    )
    await api().list_tasks(TaskQuery(status=TaskStatus.DONE))
    assert query_of(route)["filters"] == (
        "{'status':{'@nin':['pending','confirmed','in progress','canceled']}}"
    )
    await api().list_tasks(TaskQuery(status=TaskStatus.CANCELLED))
    assert "'canceled'" not in query_of(route)["filters"]
    await api().list_tasks(TaskQuery(status=TaskStatus.IN_PROGRESS))
    assert "'in progress'" not in query_of(route)["filters"]


async def test_a_status_filter_that_guesty_ignored_still_returns_only_that_status(
    router: respx.MockRouter,
) -> None:
    mock_list(router)
    page = await api().list_tasks(TaskQuery(status=TaskStatus.DONE))
    assert [t.id for t in page.items] == [INSPECTION_ID]


async def test_the_listing_filter_is_applied_here_and_reads_whole_pages(
    router: respx.MockRouter,
) -> None:
    route = mock_list(router)
    page = await api().list_tasks(TaskQuery(listing_id=LISTING_ID, limit=5))
    assert [t.id for t in page.items] == [CLEANING_ID, INSPECTION_ID]
    assert "filters" not in query_of(route) and route.calls[0].request.url.params["limit"] == "100"


async def test_the_type_and_due_date_filters_are_applied_here(router: respx.MockRouter) -> None:
    mock_list(router)
    cleaning = await api().list_tasks(TaskQuery(task_type=TaskType.CLEANING))
    assert [t.id for t in cleaning.items] == [CLEANING_ID]
    mock_list(router)
    window = await api().list_tasks(TaskQuery(due_from=date(2026, 12, 1), due_to=date(2026, 12, 2)))
    assert [t.id for t in window.items] == [CLEANING_ID]
    mock_list(router)
    early = await api().list_tasks(TaskQuery(due_to=date(2026, 11, 30)))
    assert [t.id for t in early.items] == [INSPECTION_ID]  # a task with no due time never matches


async def test_a_filtered_page_stops_at_the_limit_and_the_cursor_follows_the_last_row(
    router: respx.MockRouter,
) -> None:
    mock_list(router)
    page = await api().list_tasks(TaskQuery(listing_id=LISTING_ID, limit=1))
    assert [t.id for t in page.items] == [CLEANING_ID]
    assert page.next_cursor == encode_cursor(1)


async def test_the_scan_is_bounded_and_resumes_from_the_cursor(router: respx.MockRouter) -> None:
    other = [{"id": f"x{n}", "listing": {"listingId": "other"}} for n in range(100)]
    route = router.get(TASKS_URL).mock(return_value=json_response(other))
    page = await api().list_tasks(TaskQuery(listing_id="wanted"))
    assert page.items == [] and route.call_count == tasks.MAX_SCAN_PAGES
    assert page.next_cursor == encode_cursor(100 * tasks.MAX_SCAN_PAGES)


@pytest.mark.parametrize("field", ["status", "task_type"])
async def test_an_unknown_filter_value_is_refused_before_any_request(
    router: respx.MockRouter, field: str
) -> None:
    route = router.get(TASKS_URL).mock(return_value=json_response([]))
    with pytest.raises(PMSValidationError, match="unknown"):
        await api().list_tasks(TaskQuery.model_validate({field: "unknown"}))
    assert route.call_count == 0


@pytest.mark.parametrize("answer", [{"data": "x"}, {"data": {"results": 3}}, "text", {}, 5])
async def test_a_list_that_cannot_be_understood_is_an_upstream_error(
    router: respx.MockRouter, answer: object
) -> None:
    router.get(TASKS_URL).mock(return_value=json_response(answer))
    with pytest.raises(PMSUpstreamError, match="could not be understood"):
        await api().list_tasks(TaskQuery())


@pytest.mark.parametrize(
    "answer",
    [{"data": [{"id": "a"}]}, {"data": {"results": [{"id": "a"}]}}, {"results": [{"id": "a"}]}],
)
async def test_an_enveloped_list_is_also_read(router: respx.MockRouter, answer: object) -> None:
    router.get(TASKS_URL).mock(side_effect=[json_response(answer), json_response([])])
    page = await api().list_tasks(TaskQuery())
    assert [t.id for t in page.items] == ["a"]


async def test_more_tasks_than_a_page_is_an_upstream_error(router: respx.MockRouter) -> None:
    router.get(TASKS_URL).mock(return_value=json_response([{"id": str(n)} for n in range(101)]))
    with pytest.raises(PMSUpstreamError, match="more tasks than expected"):
        await api().list_tasks(TaskQuery())


async def test_a_bad_cursor_is_refused_before_any_request(router: respx.MockRouter) -> None:
    route = router.get(TASKS_URL).mock(return_value=json_response([]))
    with pytest.raises(PMSValidationError, match="cursor"):
        await api().list_tasks(TaskQuery(cursor="not-a-cursor"))
    assert route.call_count == 0


async def test_the_list_is_read_again_after_a_temporary_failure(router: respx.MockRouter) -> None:
    route = router.get(TASKS_URL).mock(
        side_effect=[httpx.Response(503), json_response([{"id": "a"}]), json_response([])]
    )
    page = await api().list_tasks(TaskQuery())
    assert [t.id for t in page.items] == ["a"] and route.call_count == 3


# ---- create_task: the request ----


async def test_a_task_is_one_post_with_the_documented_body(router: respx.MockRouter) -> None:
    post = router.post(CREATE_URL).mock(return_value=json_response(fixture("create_response.json")))
    due = future()
    created = await api().create_task(
        new_task(
            description="Kitchen first.",
            task_type=TaskType.CLEANING,
            checklist=["Wash the linen", "Restock soap"],
            listing_id=LISTING_ID,
            reservation_id=RESERVATION_ID,
            due_at=due,
        )
    )
    assert post.call_count == 1
    body = json.loads(post.calls.last.request.content)
    assert set(body) == {
        "title",
        "description",
        "type",
        "checklist",
        "listingId",
        "reservationId",
        "canStartAfter",
        "mustFinishBefore",
    }
    assert body["title"] == "Deep clean" and body["type"] == "cleaning"
    assert body["checklist"] == ["Wash the linen", "Restock soap"]
    assert body["listingId"] == LISTING_ID and body["reservationId"] == RESERVATION_ID
    assert body["mustFinishBefore"] == due.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert body["canStartAfter"] < body["mustFinishBefore"] and body["canStartAfter"].endswith("Z")
    assert created.id == CREATED_ID and created.checklist[0].text == "Wash the linen"


async def test_a_minimal_task_sends_only_the_title(router: respx.MockRouter) -> None:
    post = router.post(CREATE_URL).mock(return_value=json_response({"_id": "a", "title": "T"}))
    await api().create_task(new_task())
    assert json.loads(post.calls.last.request.content) == {"title": "Deep clean"}


@pytest.mark.parametrize(
    ("kind", "sent"),
    [
        (TaskType.CLEANING, "cleaning"),
        (TaskType.MAINTENANCE, "maintenance"),
        (TaskType.INSPECTION, "inspection"),
        (TaskType.CHECK_IN, "check-in"),
        (TaskType.OTHER, None),
    ],
)
async def test_every_kind_is_written_the_way_guesty_documents_it(
    router: respx.MockRouter, kind: TaskType, sent: str | None
) -> None:
    post = router.post(CREATE_URL).mock(return_value=json_response({"_id": "a"}))
    await api().create_task(new_task(task_type=kind))
    assert json.loads(post.calls.last.request.content).get("type") == sent


@pytest.mark.parametrize("kind", [TaskType.CHECK_OUT, TaskType.UNKNOWN])
async def test_a_kind_guesty_cannot_store_is_refused_and_nothing_is_sent(
    router: respx.MockRouter, kind: TaskType
) -> None:
    post = router.post(CREATE_URL).mock(return_value=json_response({"_id": "a"}))
    with pytest.raises(PMSValidationError, match="not available"):
        await api().create_task(new_task(task_type=kind))
    assert post.call_count == 0


async def test_a_due_time_in_the_past_is_refused_and_nothing_is_sent(
    router: respx.MockRouter,
) -> None:
    post = router.post(CREATE_URL).mock(return_value=json_response({"_id": "a"}))
    with pytest.raises(PMSValidationError, match="in the past"):
        await api().create_task(new_task(due_at=future(-1)))
    assert post.call_count == 0


def test_a_due_time_in_another_zone_is_written_in_utc() -> None:
    now = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    zone = timezone(timedelta(hours=2))
    body = tasks.create_body(new_task(due_at=datetime(2026, 12, 2, 12, 0, tzinfo=zone)), now=now)
    assert body["mustFinishBefore"] == "2026-12-02T10:00:00.000Z"
    assert body["canStartAfter"] == "2026-12-01T00:00:00.000Z"


async def test_no_task_is_sent_when_the_deadline_is_too_close(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    post = router.post(CREATE_URL).mock(return_value=json_response({"_id": "a"}))
    monkeypatch.setattr(tasks, "remaining_seconds", lambda: 0.5)
    with pytest.raises(PMSUpstreamError, match="Nothing was created"):
        await api().create_task(new_task())
    assert post.call_count == 0


# ---- create_task: failures never send twice ----


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("slow"), httpx.ReadError("dropped"), httpx.WriteTimeout("w")]
)
async def test_a_timeout_or_drop_is_never_resent_and_the_outcome_is_unknown(
    router: respx.MockRouter, failure: Exception
) -> None:
    post = router.post(CREATE_URL).mock(side_effect=failure)
    with pytest.raises(PMSUpstreamError, match="outcome is unknown"):
        await api().create_task(new_task())
    assert post.call_count == 1


async def test_a_server_error_is_never_resent(router: respx.MockRouter) -> None:
    post = router.post(CREATE_URL).mock(return_value=json_response({"message": "private"}, 503))
    with pytest.raises(PMSUpstreamError) as caught:
        await api().create_task(new_task())
    assert post.call_count == 1 and "private" not in str(caught.value)


async def test_a_rejection_is_reported_without_upstream_text_or_the_draft(
    router: respx.MockRouter,
) -> None:
    router.post(CREATE_URL).mock(return_value=json_response({"message": "private"}, 400))
    with pytest.raises(PMSValidationError) as caught:
        await api().create_task(new_task(title=DRAFT_TEXT))
    assert "private" not in str(caught.value) and DRAFT_TEXT not in str(caught.value)


async def test_a_rate_limit_without_a_wait_time_is_not_resent(router: respx.MockRouter) -> None:
    post = router.post(CREATE_URL).mock(return_value=json_response({}, 429))
    with pytest.raises(base.PMSRateLimitedError):
        await api().create_task(new_task())
    assert post.call_count == 1


async def test_an_expired_token_is_not_resent(router: respx.MockRouter) -> None:
    post = router.post(CREATE_URL).mock(return_value=json_response({}, 401))
    with pytest.raises(base.PMSError):
        await api().create_task(new_task())
    assert post.call_count == 1


@pytest.mark.parametrize("answer", [[], "text", {}, {"data": "x"}, {"_id": ""}])
async def test_an_answer_that_cannot_be_understood_says_it_was_probably_created(
    router: respx.MockRouter, answer: object
) -> None:
    post = router.post(CREATE_URL).mock(return_value=json_response(answer))
    with pytest.raises(PMSUpstreamError, match="probably created"):
        await api().create_task(new_task())
    assert post.call_count == 1


async def test_a_body_that_is_not_json_says_it_was_probably_created(
    router: respx.MockRouter,
) -> None:
    router.post(CREATE_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(PMSUpstreamError, match="probably created"):
        await api().create_task(new_task())


# ---- logs ----


async def test_logs_never_hold_task_text_drafts_or_checklist_words(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    router.get(TASKS_URL).mock(
        side_effect=[
            json_response([{"taskTitle": {"children": INJECTION_TEXT}}, {"id": "a"}]),
            json_response([]),
        ]
    )
    await api().list_tasks(TaskQuery())
    router.post(CREATE_URL).mock(return_value=json_response({"message": DRAFT_TEXT}, 500))
    with pytest.raises(PMSUpstreamError):
        await api().create_task(new_task(title=DRAFT_TEXT, checklist=[CHECKLIST_SECRET]))
    assert caplog.records
    recorded = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for private in (INJECTION_TEXT, DRAFT_TEXT, CHECKLIST_SECRET):
        assert private not in recorded


# ---- capabilities ----


def test_capabilities_and_wiring() -> None:
    assert {Capability.TASKS_READ, Capability.TASKS_CREATE} == tasks.CAPABILITIES
    assert DOMAIN_MODULES["tasks"] == "tasks"
    assert "tasks" in discover_domains()


# ---- list rows as Guesty sends them (each column wrapped) ----


def live_shaped_row() -> dict[str, object]:
    return {
        "status": {"status": "completed"},
        "taskTitle": {"children": "Fake leak repair"},
        "type": {"type": "maintenance"},
        "listing": {"listingId": "6a1000000000000000000001", "title": "Fake flat"},
        "reservation": {"reservationId": "6a2000000000000000000001", "status": "confirmed"},
        "scheduledFor": {"startTime": "2026-10-07T12:50:43.137Z", "mustFinishBefore": None},
        "endTime": {},
        "canStartAfter": {"date": None},
        "mustFinishBefore": {"date": "2026-10-08T10:00:00.000Z"},
        "assignee": {"assigneeId": "6a9000000000000000000001", "assigneeFullName": "Fake Person"},
        "id": "6a8000000000000000000t01",
    }


def test_a_wrapped_list_row_maps_status_type_due_date_and_assignee() -> None:
    found = tasks.map_task(live_shaped_row())
    assert found is not None
    assert found.status is TaskStatus.DONE and found.task_type is TaskType.MAINTENANCE
    assert found.title == "Fake leak repair"
    assert found.listing_id == "6a1000000000000000000001"
    assert found.reservation_id == "6a2000000000000000000001"
    assert found.due_at is not None and found.due_at.isoformat().startswith("2026-10-08T10:00")
    assert found.assignee_name == "Fake Person"


async def test_a_small_limit_asks_guesty_for_at_least_25_and_returns_only_the_limit(
    router: respx.MockRouter,
) -> None:
    rows = [{**live_shaped_row(), "id": f"6a80000000000000000t{n:04d}"} for n in range(25)]
    route = router.get(TASKS_URL).mock(return_value=json_response(rows))
    page = await api().list_tasks(TaskQuery(limit=3))
    assert dict(route.calls.last.request.url.params)["limit"] == "25"
    assert len(page.items) == 3 and page.next_cursor is not None
