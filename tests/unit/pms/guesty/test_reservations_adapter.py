"""The Guesty reservation adapter through the real client, with respx as the only network."""

import logging
import time
from collections.abc import AsyncIterator
from datetime import date

import httpx
import pytest
import respx
from guesty_reservation_helpers import (
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
    fixture,
    json_response,
    make_client,
    mock_detail,
    mock_router,
)

from pmsbridge.pms.base import (
    Capability,
    CapabilityNotSupportedError,
    PMSNotFoundError,
    PMSUpstreamError,
    PMSValidationError,
    call_deadline,
)
from pmsbridge.pms.guesty import reservations
from pmsbridge.pms.guesty.common import MAX_SKIP, decode_cursor, encode_cursor
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains
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
    assert query_of(routes["by_ids"]) == {"reservationIds": RESERVATION_ID}
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


# ---- add note, capabilities ----


async def test_adding_a_note_is_refused_and_sends_nothing(router: respx.MockRouter) -> None:
    route = router.route().mock(return_value=json_response({}))
    with pytest.raises(CapabilityNotSupportedError):
        await reservations.build(make_client()).add_reservation_note(
            ReservationNote(reservation_id=RESERVATION_ID, note="Please call the guest.")
        )
    assert route.call_count == 0


def test_capabilities_and_wiring() -> None:
    assert {Capability.RESERVATIONS_READ} == reservations.CAPABILITIES
    assert Capability.RESERVATIONS_ADD_NOTE not in reservations.CAPABILITIES
    assert DOMAIN_MODULES["reservations"] == "reservations"
    assert discover_domains()["reservations"] is reservations
    assert isinstance(reservations.build(make_client()), reservations.GuestyReservations)
