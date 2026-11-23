"""The Guesty reservation adapter through the real client, with respx as the only network."""

import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import date

import httpx
import pytest
import respx
from guesty_reservation_helpers import (
    BASE,
    BY_IDS_URL,
    DOOR_CODE,
    GUEST_EMAIL,
    GUEST_ID,
    GUEST_NAME,
    GUEST_NOTE,
    GUEST_PHONE,
    REPORTS_URL,
    RESERVATION_ID,
    SEARCH_URL,
    StandInTokens,
    fixture,
    json_response,
    make_client,
    mock_detail,
    mock_router,
)
from pydantic import ValidationError

from pmsbridge.pms.base import (
    WRITE_CAPABILITIES,
    Capability,
    PMSAuthError,
    PMSError,
    PMSNotFoundError,
    PMSRateLimitedError,
    PMSUpstreamError,
    PMSValidationError,
    Provider,
    call_deadline,
)
from pmsbridge.pms.guesty import reservations
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import MAX_SKIP, decode_cursor, encode_cursor
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains
from pmsbridge.pms.http import PMSHttpClient
from pmsbridge.pms.models import (
    ReservationNote,
    ReservationQuery,
    ReservationStatus,
)


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        yield mocked


def query_of(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


# ---- list ----


async def test_list_sends_the_default_parameters(router: respx.MockRouter) -> None:
    route = router.get(SEARCH_URL).mock(return_value=json_response({"results": []}))
    page = await reservations.build(make_client()).list_reservations(ReservationQuery(limit=10))
    assert page.items == [] and page.next_cursor is None and page.total is None
    assert query_of(route) == {"limit": "10", "skip": "0", "sort": "checkIn"}
    assert route.calls.last.request.headers["authorization"] == "Bearer fake-access-token"


async def test_every_query_field_becomes_its_filter(router: respx.MockRouter) -> None:
    route = router.get(SEARCH_URL).mock(return_value=json_response({"results": []}))
    query = ReservationQuery(
        listing_id="6a1f00000000000000000001",
        statuses=[ReservationStatus.PENDING, ReservationStatus.CANCELLED],
        search="TST-1001",
        check_in_from=date(2027, 3, 1),
        check_in_to=date(2027, 3, 31),
        check_out_from=date(2027, 3, 2),
        check_out_to=date(2027, 4, 2),
        created_from=date(2027, 1, 1),
        created_to=date(2027, 1, 31),
    )
    await reservations.build(make_client()).list_reservations(query)
    assert query_of(route) == {
        "filter[listingId]": "6a1f00000000000000000001",
        "filter[status]": "awaiting_payment,reserved,canceled",
        "filter[confirmationCode]": "TST-1001",
        "filter[checkIn][gte]": "2027-03-01",
        "filter[checkIn][lte]": "2027-03-31",
        "filter[checkOut][gte]": "2027-03-02",
        "filter[checkOut][lte]": "2027-04-02",
        "filter[createdAt][gte]": "2027-01-01T00:00:00.000Z",
        "filter[createdAt][lt]": "2027-02-01T00:00:00.000Z",
        "sort": "checkIn",
        "limit": "25",
        "skip": "0",
    }


def test_the_status_filter_uses_the_reverse_map_without_repeats() -> None:
    filters = reservations.build_filters(
        ReservationQuery(
            statuses=[
                ReservationStatus.CHECKED_OUT,
                ReservationStatus.UNKNOWN,
                ReservationStatus.CHECKED_OUT,
            ]
        )
    )
    assert filters == {"filter[status]": "checked_out,completed,closed,alteration"}
    assert reservations.build_filters(ReservationQuery(statuses=[])) == {}
    assert reservations.build_filters(ReservationQuery()) == {}


def test_created_to_includes_the_whole_last_day_even_at_a_month_end() -> None:
    filters = reservations.build_filters(ReservationQuery(created_to=date(2027, 12, 31)))
    assert filters == {"filter[createdAt][lt]": "2028-01-01T00:00:00.000Z"}


async def test_a_page_has_a_cursor_while_guesty_says_there_is_more(
    router: respx.MockRouter,
) -> None:
    route = router.get(SEARCH_URL).mock(return_value=json_response(fixture("search_page.json")))
    api = reservations.build(make_client())
    first = await api.list_reservations(ReservationQuery(limit=3))
    assert [r.confirmation_code for r in first.items] == ["TST-1001", "TST-1002", "TST-1003"]
    assert [r.status for r in first.items] == [
        ReservationStatus.CONFIRMED,
        ReservationStatus.CANCELLED,
        ReservationStatus.CHECKED_IN,
    ]
    assert first.next_cursor is not None and decode_cursor(first.next_cursor) == 3
    assert all(r.guest is None and r.balance_due is None for r in first.items)
    route.mock(return_value=json_response(fixture("search_last_page.json")))
    last = await api.list_reservations(ReservationQuery(limit=3, cursor=first.next_cursor))
    assert query_of(route)["skip"] == "3"
    assert [r.status for r in last.items] == [ReservationStatus.PENDING]
    assert last.next_cursor is None


async def test_without_has_more_a_full_page_may_have_more(router: respx.MockRouter) -> None:
    items = [
        {
            "reservationId": f"r{n}",
            "checkInDateLocalized": "2027-01-01",
            "checkOutDateLocalized": "2027-01-02",
            "stay": [{"listingId": "l1"}],
        }
        for n in range(2)
    ]
    router.get(SEARCH_URL).mock(return_value=json_response(items))
    api = reservations.build(make_client())
    full = await api.list_reservations(ReservationQuery(limit=2))
    assert decode_cursor(full.next_cursor) == 2
    short = await api.list_reservations(ReservationQuery(limit=3))
    assert short.next_cursor is None


async def test_malformed_items_are_skipped_but_still_counted(router: respx.MockRouter) -> None:
    router.get(SEARCH_URL).mock(return_value=json_response(fixture("search_with_malformed.json")))
    page = await reservations.build(make_client()).list_reservations(ReservationQuery(limit=5))
    assert [r.id for r in page.items] == ["6a2000000000000000000013"]
    assert page.next_cursor is None


async def test_a_response_that_is_not_a_list_is_an_upstream_error(
    router: respx.MockRouter,
) -> None:
    router.get(SEARCH_URL).mock(return_value=json_response({"unexpected": True}))
    with pytest.raises(PMSUpstreamError):
        await reservations.build(make_client()).list_reservations(ReservationQuery())


async def test_a_bad_cursor_is_refused_before_any_request(router: respx.MockRouter) -> None:
    route = router.get(SEARCH_URL).mock(return_value=json_response({"results": []}))
    api = reservations.build(make_client())
    for cursor in ("not-a-cursor", encode_cursor(MAX_SKIP + 1), "9"):
        with pytest.raises(PMSValidationError):
            await api.list_reservations(ReservationQuery(cursor=cursor))
    assert route.call_count == 0


# ---- get ----


async def test_get_combines_the_reservation_the_guest_and_the_balance(
    router: respx.MockRouter,
) -> None:
    routes = mock_detail(router)
    reservation = await reservations.build(make_client()).get_reservation(f" {RESERVATION_ID} ")
    assert query_of(routes["by_ids"]) == {"reservationIds[]": RESERVATION_ID}
    assert query_of(routes["guest"]) == {"fields": "fullName email phone"}
    assert query_of(routes["report"]) == {"filter[confirmationCode]": "TST-1001", "limit": "10"}
    assert reservation.id == RESERVATION_ID and reservation.listing_id == "6a1f00000000000000000001"
    assert reservation.guest is not None
    assert reservation.guest.name == GUEST_NAME
    assert (reservation.guest.email, reservation.guest.phone) == (GUEST_EMAIL, GUEST_PHONE)
    assert reservation.balance_due is not None
    assert (str(reservation.balance_due.amount), reservation.balance_due.currency) == (
        "125.5",
        "EUR",
    )
    assert reservation.total_price is None
    shown = reservation.model_dump_json()
    assert DOOR_CODE not in shown and "FAKE-CARD" not in shown
    assert reservation.notes == f"Guest note: {GUEST_NOTE}"


async def test_get_never_asks_for_payment_or_door_data(router: respx.MockRouter) -> None:
    routes = mock_detail(router)
    await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    for route in routes.values():
        url = str(route.calls.last.request.url).lower()
        for word in ("keycode", "payment", "card", "payout", "bank", "vcc"):
            assert word not in url


async def test_get_with_a_missing_guest_still_returns(router: respx.MockRouter) -> None:
    mock_detail(router, guest=json_response({"message": "private text"}, 404))
    reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert reservation.guest is None and reservation.balance_due is not None


async def test_get_with_a_failing_report_still_returns(router: respx.MockRouter) -> None:
    mock_detail(router, report=json_response({"message": "boom"}, 500))
    reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert reservation.balance_due is None
    assert reservation.guest is not None and reservation.guest.name == GUEST_NAME


async def test_get_with_an_empty_report_still_returns(router: respx.MockRouter) -> None:
    mock_detail(router, report=json_response(fixture("report_no_match.json")))
    reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert reservation.balance_due is None and reservation.guest is not None


async def test_get_when_both_extras_fail_still_returns(router: respx.MockRouter) -> None:
    mock_detail(router, guest=json_response({}, 403), report=json_response({"results": "oops"}))
    reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert reservation.guest is None and reservation.balance_due is None
    assert reservation.confirmation_code == "TST-1001"


async def test_a_balance_without_a_currency_is_left_out(router: respx.MockRouter) -> None:
    report = {"results": [{"reservationId": RESERVATION_ID, "money": {"balanceDue": 5}}]}
    mock_detail(router, report=json_response(report))
    reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert reservation.balance_due is None


async def test_a_guest_record_with_nothing_usable_gives_no_guest(
    router: respx.MockRouter,
) -> None:
    mock_detail(router, guest=json_response({"_id": GUEST_ID, "fullName": " "}))
    reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert reservation.guest is None


async def test_a_guest_with_only_an_email_gets_the_unknown_name(
    router: respx.MockRouter,
) -> None:
    mock_detail(router, guest=json_response({"email": GUEST_EMAIL}))
    reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert reservation.guest is not None
    assert reservation.guest.name == "Unknown" and reservation.guest.email == GUEST_EMAIL


async def test_without_a_confirmation_code_the_report_is_not_asked(
    router: respx.MockRouter,
) -> None:
    body = fixture("by_ids.json")
    assert isinstance(body, list)
    body[0].pop("confirmationCode")
    routes = mock_detail(router, by_ids=body)
    reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert reservation.balance_due is None and routes["report"].call_count == 0


async def test_without_a_guest_id_the_guest_is_not_asked(router: respx.MockRouter) -> None:
    body = fixture("by_ids.json")
    assert isinstance(body, list)
    body[0].pop("bookerId")
    routes = mock_detail(router, by_ids=body)
    reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert reservation.guest is None and routes["guest"].call_count == 0


async def test_a_by_ids_answer_in_an_envelope_is_read_too(router: respx.MockRouter) -> None:
    body = {"results": fixture("by_ids.json")}
    routes = mock_detail(router, by_ids=body)
    reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert reservation.id == RESERVATION_ID and routes["guest"].call_count == 1


async def test_time_running_out_after_the_reservation_was_read_leaves_the_extras_empty(
    router: respx.MockRouter,
) -> None:
    routes = mock_detail(router)

    def answer_then_run_out_of_time(request: httpx.Request) -> httpx.Response:
        call_deadline.set(time.monotonic() - 1)  # the deadline passes right after this call
        return json_response(fixture("by_ids.json"))

    routes["by_ids"].mock(side_effect=answer_then_run_out_of_time)
    token = call_deadline.set(time.monotonic() + 25)
    try:
        reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    finally:
        call_deadline.reset(token)
    assert reservation.id == RESERVATION_ID and reservation.confirmation_code == "TST-1001"
    assert reservation.guest is None and reservation.balance_due is None
    assert routes["guest"].call_count == 0 and routes["report"].call_count == 0


async def test_a_look_up_is_not_retried_past_the_deadline(router: respx.MockRouter) -> None:
    routes = mock_detail(router)

    def fail_and_run_out_of_time(request: httpx.Request) -> httpx.Response:
        call_deadline.set(time.monotonic() - 1)
        return json_response({"message": "busy"}, 503)

    routes["guest"].mock(side_effect=fail_and_run_out_of_time)
    token = call_deadline.set(time.monotonic() + 25)
    try:
        reservation = await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    finally:
        call_deadline.reset(token)
    assert reservation.guest is None and reservation.balance_due is None
    assert routes["guest"].call_count == 1  # no retry once the time is gone
    assert routes["report"].call_count == 0


async def test_an_empty_answer_or_a_404_is_not_found(router: respx.MockRouter) -> None:
    route = router.get(BY_IDS_URL).mock(return_value=json_response([]))
    api = reservations.build(make_client())
    with pytest.raises(PMSNotFoundError):
        await api.get_reservation("6a20000000000000000000ff")
    route.mock(return_value=json_response({"message": "private text"}, 404))
    with pytest.raises(PMSNotFoundError):
        await api.get_reservation("6a20000000000000000000ff")


async def test_an_answer_for_another_reservation_is_not_found(router: respx.MockRouter) -> None:
    router.get(BY_IDS_URL).mock(return_value=json_response(fixture("by_ids.json")))
    with pytest.raises(PMSNotFoundError):
        await reservations.build(make_client()).get_reservation("6a20000000000000000000ff")


async def test_an_unreadable_reservation_is_an_upstream_error(router: respx.MockRouter) -> None:
    router.get(BY_IDS_URL).mock(return_value=json_response({"nope": 1}))
    api = reservations.build(make_client())
    with pytest.raises(PMSUpstreamError):
        await api.get_reservation(RESERVATION_ID)
    router.get(BY_IDS_URL).mock(
        return_value=json_response([{"_id": RESERVATION_ID, "status": "confirmed"}])
    )
    with pytest.raises(PMSUpstreamError):
        await api.get_reservation(RESERVATION_ID)


async def test_only_one_id_is_ever_sent(router: respx.MockRouter) -> None:
    route = router.get(BY_IDS_URL).mock(return_value=json_response([]))
    api = reservations.build(make_client())
    for hostile in ("a,b", "", "   ", "x" * 201):
        with pytest.raises(PMSValidationError):
            await api.get_reservation(hostile)
    assert route.call_count == 0


async def test_the_guest_id_is_quoted_in_the_path(router: respx.MockRouter) -> None:
    body = fixture("by_ids.json")
    assert isinstance(body, list)
    body[0]["bookerId"] = "a/b"
    guest = router.get(url__regex=r".*/guests-crud/.*").mock(return_value=json_response({}))
    router.get(BY_IDS_URL).mock(return_value=json_response(body))
    router.get(REPORTS_URL).mock(return_value=json_response({"results": []}))
    await reservations.build(make_client()).get_reservation(RESERVATION_ID)
    assert guest.calls.last.request.url.raw_path.decode().startswith("/v1/guests-crud/a%2Fb")


async def test_upstream_errors_on_the_main_call_surface(router: respx.MockRouter) -> None:
    router.get(BY_IDS_URL).mock(return_value=httpx.Response(400, json={"message": "bad"}))
    with pytest.raises(PMSValidationError):
        await reservations.build(make_client()).get_reservation(RESERVATION_ID)


# ---- logs ----


async def test_logs_never_hold_guest_details_or_notes(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    mock_detail(router, guest=json_response(fixture("guest.json")), report=json_response({}, 500))
    api = reservations.build(make_client())
    await api.get_reservation(RESERVATION_ID)
    router.get(SEARCH_URL).mock(return_value=json_response(fixture("search_page.json")))
    await api.list_reservations(ReservationQuery(limit=3))
    router.get(SEARCH_URL).mock(return_value=json_response(fixture("search_with_malformed.json")))
    await api.list_reservations(ReservationQuery(limit=5))
    assert caplog.records
    recorded = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for private in (
        GUEST_NAME,
        GUEST_EMAIL,
        GUEST_PHONE,
        GUEST_NOTE,
        DOOR_CODE,
        "Extra towels",
        "Repeat guest",
    ):
        assert private not in recorded


# ---- add note ----

NOTE_TEXT = "Guest asked for a late checkout."
COMMENTS_URL = f"{BY_IDS_URL}/{RESERVATION_ID}/comments"


def note_of(text: str = NOTE_TEXT, reservation_id: str = RESERVATION_ID) -> ReservationNote:
    return ReservationNote(reservation_id=reservation_id, note=text)


def mock_comment(router: respx.MockRouter, answer: httpx.Response | None = None) -> respx.Route:
    return router.post(COMMENTS_URL).mock(
        return_value=answer or json_response(fixture("comment_response.json"), 201)
    )


async def test_a_note_is_one_post_of_the_text_alone_then_a_read(router: respx.MockRouter) -> None:
    comment = mock_comment(router)
    by_ids = router.get(BY_IDS_URL).mock(return_value=json_response(fixture("by_ids.json")))
    reservation = await reservations.build(make_client()).add_reservation_note(note_of())
    assert comment.call_count == 1 and by_ids.call_count == 1
    request = comment.calls.last.request
    assert json.loads(request.content) == {"body": NOTE_TEXT}  # no mentions, parent or notes keys
    assert request.headers["authorization"] == "Bearer fake-access-token"
    assert dict(by_ids.calls.last.request.url.params) == {"reservationIds[]": RESERVATION_ID}
    assert reservation.id == RESERVATION_ID and reservation.confirmation_code == "TST-1001"
    assert DOOR_CODE not in reservation.model_dump_json()


async def test_a_note_never_writes_the_notes_fields_or_the_door_code(
    router: respx.MockRouter,
) -> None:
    mock_comment(router)
    router.get(BY_IDS_URL).mock(return_value=json_response(fixture("by_ids.json")))
    others = router.route(method__in=["PUT", "PATCH", "DELETE"]).mock(
        return_value=json_response({})
    )
    await reservations.build(make_client()).add_reservation_note(note_of())
    assert others.call_count == 0
    sent = [call.request for call in router.calls if call.request.method == "POST"]
    assert len(sent) == 1 and "keyCode" not in sent[0].content.decode()


async def test_the_note_text_is_trimmed_and_sent_whole(router: respx.MockRouter) -> None:
    comment = mock_comment(router)
    router.get(BY_IDS_URL).mock(return_value=json_response(fixture("by_ids.json")))
    text = 'Line one.\nLine two with "quotes" and <tags>.'
    await reservations.build(make_client()).add_reservation_note(note_of(f"  {text}  "))
    assert json.loads(comment.calls.last.request.content) == {"body": text}


@pytest.mark.parametrize("text", [" ", "\n\t "])
async def test_a_blank_note_is_refused_and_sends_nothing(
    router: respx.MockRouter, text: str
) -> None:
    route = router.route().mock(return_value=json_response({}))
    with pytest.raises(ValidationError):  # the canonical model already trims and refuses it
        note_of(text)
    unchecked = ReservationNote.model_construct(reservation_id=RESERVATION_ID, note=text)
    with pytest.raises(PMSValidationError):
        await reservations.build(make_client()).add_reservation_note(unchecked)
    assert route.call_count == 0


async def test_a_note_longer_than_guesty_allows_is_refused_and_sends_nothing(
    router: respx.MockRouter,
) -> None:
    comment = mock_comment(router)
    router.get(BY_IDS_URL).mock(return_value=json_response(fixture("by_ids.json")))
    api = reservations.build(make_client())
    with pytest.raises(PMSValidationError) as caught:
        await api.add_reservation_note(note_of("x" * (reservations.MAX_COMMENT_LENGTH + 1)))
    assert "x" * 50 not in caught.value.message and comment.call_count == 0
    await api.add_reservation_note(note_of("x" * reservations.MAX_COMMENT_LENGTH))
    assert comment.call_count == 1


async def test_a_bad_id_is_refused_and_a_hostile_one_stays_in_the_path(
    router: respx.MockRouter,
) -> None:
    posts = router.post(url__regex=r".*/comments$").mock(return_value=json_response({}, 404))
    api = reservations.build(make_client())
    with pytest.raises(PMSValidationError):
        await api.add_reservation_note(note_of(reservation_id="a,b"))
    assert posts.call_count == 0
    with pytest.raises(PMSNotFoundError):
        await api.add_reservation_note(note_of(reservation_id="a/b?x=1"))
    sent = posts.calls.last.request
    assert sent.url.raw_path.decode() == "/v1/reservations-v3/a%2Fb%3Fx%3D1/comments"


async def test_an_unknown_reservation_is_not_found_and_nothing_is_read_after(
    router: respx.MockRouter,
) -> None:
    comment = mock_comment(router, json_response({"message": "private upstream"}, 404))
    by_ids = router.get(BY_IDS_URL).mock(return_value=json_response(fixture("by_ids.json")))
    with pytest.raises(PMSNotFoundError) as caught:
        await reservations.build(make_client()).add_reservation_note(note_of())
    assert "private upstream" not in caught.value.message
    assert comment.call_count == 1 and by_ids.call_count == 0


@pytest.mark.parametrize("status", [400, 403, 422, 500])
async def test_a_refused_note_is_not_sent_again_and_hides_the_upstream_text(
    router: respx.MockRouter, status: int
) -> None:
    comment = mock_comment(router, json_response({"message": "private upstream"}, status))
    by_ids = router.get(BY_IDS_URL).mock(return_value=json_response(fixture("by_ids.json")))
    with pytest.raises(PMSError) as caught:
        await reservations.build(make_client()).add_reservation_note(note_of())
    assert "private upstream" not in caught.value.message and NOTE_TEXT not in caught.value.message
    assert comment.call_count == 1 and by_ids.call_count == 0


async def test_a_timeout_means_the_outcome_is_unknown_and_nothing_is_sent_again(
    router: respx.MockRouter,
) -> None:
    comment = router.post(COMMENTS_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    by_ids = router.get(BY_IDS_URL).mock(return_value=json_response(fixture("by_ids.json")))
    with pytest.raises(PMSUpstreamError) as caught:
        await reservations.build(make_client()).add_reservation_note(note_of())
    assert "outcome is unknown" in caught.value.message
    assert comment.call_count == 1 and by_ids.call_count == 0


async def test_a_429_without_a_wait_time_is_not_sent_again(router: respx.MockRouter) -> None:
    comment = mock_comment(router, httpx.Response(429))
    with pytest.raises(PMSRateLimitedError):
        await reservations.build(make_client()).add_reservation_note(note_of())
    assert comment.call_count == 1


async def test_a_429_with_a_wait_time_is_sent_again_once_after_the_wait(
    router: respx.MockRouter,
) -> None:
    comment = router.post(COMMENTS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            json_response(fixture("comment_response.json"), 201),
        ]
    )
    router.get(BY_IDS_URL).mock(return_value=json_response(fixture("by_ids.json")))
    await reservations.build(make_client()).add_reservation_note(note_of())
    assert comment.call_count == 2  # the first was not processed


async def test_an_expired_token_on_a_note_is_renewed_but_the_note_is_not_resent(
    router: respx.MockRouter,
) -> None:
    class Renewing(StandInTokens):
        async def refresh_after_rejection(self, rejected: str) -> str | None:
            return "renewed-token"

    comment = mock_comment(router, httpx.Response(401))
    http = PMSHttpClient(Provider.GUESTY, BASE)
    client = GuestyClient(Renewing(), "conn-guesty-tests", http)  # type: ignore[arg-type]
    with pytest.raises(PMSAuthError) as caught:
        await reservations.build(client).add_reservation_note(note_of())
    assert "Nothing was changed" in caught.value.message and comment.call_count == 1


async def test_a_failed_read_after_the_note_says_it_was_added(router: respx.MockRouter) -> None:
    comment = mock_comment(router)
    router.get(BY_IDS_URL).mock(return_value=json_response({"message": "private"}, 500))
    with pytest.raises(PMSUpstreamError) as caught:
        await reservations.build(make_client()).add_reservation_note(note_of())
    assert caught.value.message == (
        "The note was added to the reservation, but the reservation could not be read back "
        "afterwards. Do not add the note again."
    )
    assert comment.call_count == 1


async def test_a_read_that_no_longer_finds_the_reservation_also_says_it_was_added(
    router: respx.MockRouter,
) -> None:
    comment = mock_comment(router)
    router.get(BY_IDS_URL).mock(return_value=json_response([]))
    with pytest.raises(PMSUpstreamError, match="Do not add the note again"):
        await reservations.build(make_client()).add_reservation_note(note_of())
    assert comment.call_count == 1


async def test_too_little_time_left_sends_nothing(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = router.route().mock(return_value=json_response({}))
    monkeypatch.setattr(reservations, "remaining_seconds", lambda: 0.5)
    with pytest.raises(PMSUpstreamError, match="ran out"):
        await reservations.build(make_client()).add_reservation_note(note_of())
    assert route.call_count == 0


async def test_a_deadline_that_has_passed_sends_nothing(router: respx.MockRouter) -> None:
    route = router.route().mock(return_value=json_response({}))
    token = call_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(PMSUpstreamError, match="ran out"):
            await reservations.build(make_client()).add_reservation_note(note_of())
    finally:
        call_deadline.reset(token)
    assert route.call_count == 0


async def test_the_note_text_never_reaches_the_log(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    mock_comment(router, json_response({"message": NOTE_TEXT}, 500))
    with pytest.raises(PMSError):
        await reservations.build(make_client()).add_reservation_note(note_of())
    mock_comment(router)
    router.get(BY_IDS_URL).mock(return_value=json_response({}, 500))
    with pytest.raises(PMSError):
        await reservations.build(make_client()).add_reservation_note(note_of())
    assert caplog.records
    recorded = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert NOTE_TEXT not in recorded and "late checkout" not in recorded


# ---- capabilities ----


def test_capabilities_and_wiring() -> None:
    assert {
        Capability.RESERVATIONS_READ,
        Capability.RESERVATIONS_ADD_NOTE,
    } == reservations.CAPABILITIES
    assert Capability.RESERVATIONS_ADD_NOTE in WRITE_CAPABILITIES
    assert DOMAIN_MODULES["reservations"] == "reservations"
    assert discover_domains()["reservations"] is reservations
    assert isinstance(reservations.build(make_client()), reservations.GuestyReservations)


# ---- money in a by-ids result ----


def by_ids_item(money: dict[str, object]) -> dict[str, object]:
    """A by-ids result as Guesty sends it (`_id`, `stay[].unitId`, `bookerId`), with `money`."""
    return {
        "_id": RESERVATION_ID,
        "status": "confirmed",
        "platform": "airbnb2",
        "bookerId": GUEST_ID,
        "confirmationCode": "HMFAKE0001",
        "createdAt": "2026-10-05T02:30:42.300Z",
        "notes": {},
        "stay": [
            {
                "unitId": "6a1000000000000000000001",
                "checkInDateLocalized": "2026-10-05",
                "checkOutDateLocalized": "2026-10-07",
                "guestsCount": 2,
            }
        ],
        "money": money,
    }


def test_a_by_ids_result_gives_host_payout_and_amount_paid_but_no_payments() -> None:
    found = reservations.map_reservation(
        by_ids_item(
            {
                "currency": "aed",
                "hostPayout": 963.3,
                "totalPaid": 963.3,
                "payments": [{"amount": 963.3, "paymentMethodId": "fake-method-id"}],
            }
        )
    )
    assert found is not None
    assert found.listing_id == "6a1000000000000000000001" and found.guest_id == GUEST_ID
    assert found.nights == 2 and found.channel == "airbnb2"
    assert found.extras["host_payout"] == 963.3 and found.extras["total_paid"] == 963.3
    assert found.extras["currency"] == "AED"
    assert found.total_price is None  # a channel booking without the guest's total
    assert "fake-method-id" not in found.model_dump_json()


def test_a_by_ids_result_with_the_guests_total_sets_the_total_price() -> None:
    found = reservations.map_reservation(
        by_ids_item({"currency": "USD", "totalPrice": 1250.5, "hostPayout": 1100})
    )
    assert found is not None and found.total_price is not None
    assert str(found.total_price.amount) == "1250.5" and found.total_price.currency == "USD"


# ---- get by confirmation code ----


def code_search_answer(*ids: str) -> dict[str, object]:
    return {
        "results": [{"reservationId": found, "confirmationCode": "HMFAKE0001"} for found in ids],
        "pagination": {"skip": 0, "limit": 2, "hasMore": False},
    }


async def test_get_by_confirmation_code_finds_the_id_first(router: respx.MockRouter) -> None:
    search = router.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=code_search_answer(RESERVATION_ID))
    )
    by_ids = router.get(BY_IDS_URL).mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(PMSNotFoundError):  # the by-ids answer is empty in this test
        await reservations.build(make_client()).get_reservation("HMFAKE0001")
    assert query_of(search) == {"filter[confirmationCode]": "HMFAKE0001", "limit": "2"}
    assert query_of(by_ids) == {"reservationIds[]": RESERVATION_ID}


@pytest.mark.parametrize("ids", [(), ("6a2000000000000000000001", "6a2000000000000000000002")])
async def test_a_code_matching_no_or_several_reservations_is_not_found(
    router: respx.MockRouter, ids: tuple[str, ...]
) -> None:
    router.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=code_search_answer(*ids)))
    by_ids = router.get(BY_IDS_URL).mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(PMSNotFoundError):
        await reservations.build(make_client()).get_reservation("HMFAKE0001")
    assert by_ids.call_count == 0
