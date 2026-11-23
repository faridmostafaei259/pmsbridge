"""The Guesty inbox adapter through the real client, with respx as the only network."""

import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from guesty_inbox_helpers import (
    CONVERSATION_ID,
    CONVERSATIONS_URL,
    GUEST_EMAIL,
    GUEST_NAME,
    INJECTION_TEXT,
    NEVER_SHOWN,
    POSTS_URL,
    SEND_URL,
    fixture,
    json_response,
    make_client,
    mock_router,
)

from pmsbridge.pms import base
from pmsbridge.pms.base import (
    Capability,
    CapabilityNotSupportedError,
    PMSNotFoundError,
    PMSUpstreamError,
    PMSValidationError,
)
from pmsbridge.pms.guesty import inbox
from pmsbridge.pms.guesty.common import encode_token_cursor
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains
from pmsbridge.pms.models import (
    ConversationNote,
    ConversationQuery,
    MessageChannel,
    MessageDirection,
    OutgoingMessage,
    PageRequest,
)


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        yield mocked


def query_of(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


def api() -> inbox.GuestyInbox:
    return inbox.build(make_client())


def outgoing(
    body: str = "Welcome!", channel: MessageChannel = MessageChannel.DEFAULT
) -> OutgoingMessage:
    return OutgoingMessage(conversation_id=CONVERSATION_ID, body=body, channel=channel)


# ---- list_conversations ----


async def test_list_sends_the_default_parameters(router: respx.MockRouter) -> None:
    route = router.get(CONVERSATIONS_URL).mock(
        return_value=json_response({"data": {"conversations": []}})
    )
    page = await api().list_conversations(ConversationQuery(limit=10))
    assert page.items == [] and page.next_cursor is None and page.total is None
    assert query_of(route) == {
        "type": "guest",
        "limit": "10",
        "sort": "-modifiedAt",
        "fields": inbox.CONVERSATION_FIELDS,
    }
    assert "modifiedAt" in inbox.CONVERSATION_FIELDS.split()  # else Guesty sends no cursor
    assert route.calls.last.request.headers["authorization"] == "Bearer fake-access-token"


async def test_list_maps_the_page_and_wraps_the_cursor(router: respx.MockRouter) -> None:
    route = router.get(CONVERSATIONS_URL).mock(
        return_value=json_response(fixture("conversations_page.json"))
    )
    page = await api().list_conversations(ConversationQuery(limit=3))
    assert page.items[0].id == CONVERSATION_ID and len(page.items) == 3
    assert page.items[0].guest_name == GUEST_NAME
    assert page.next_cursor == encode_token_cursor("ZmFrZS1jdXJzb3ItMQ")
    shown = page.model_dump_json()
    for private in NEVER_SHOWN:
        assert private not in shown
    await api().list_conversations(ConversationQuery(limit=3, cursor=page.next_cursor))
    assert query_of(route)["cursorAfter"] == "ZmFrZS1jdXJzb3ItMQ"
    assert "cursorBefore" not in query_of(route)


async def test_list_filters_are_sent_as_json(router: respx.MockRouter) -> None:
    route = router.get(CONVERSATIONS_URL).mock(
        return_value=json_response({"data": {"conversations": []}})
    )
    await api().list_conversations(
        ConversationQuery(reservation_id=" r1 ", listing_id="l1", unread_only=True)
    )
    assert json.loads(query_of(route)["filters"]) == [
        {"field": "reservation._id", "operator": "$eq", "value": "r1"},
        {"field": "listing._id", "operator": "$eq", "value": "l1"},
        {"field": "read", "operator": "$eq", "value": False},
    ]
    await api().list_conversations(ConversationQuery(unread_only=False))
    assert "filters" not in query_of(route)


async def test_the_end_of_the_list_gives_no_cursor(router: respx.MockRouter) -> None:
    body = {"data": {"conversations": [{"_id": "c1"}], "cursor": {"after": "", "before": ""}}}
    router.get(CONVERSATIONS_URL).mock(return_value=json_response(body))
    assert (await api().list_conversations(ConversationQuery())).next_cursor is None
    body = {"data": {"conversations": [], "cursor": {"after": "still-here"}}}
    router.get(CONVERSATIONS_URL).mock(return_value=json_response(body))
    assert (await api().list_conversations(ConversationQuery())).next_cursor is None


async def test_a_cursor_from_guesty_that_cannot_be_used_ends_the_paging(
    router: respx.MockRouter,
) -> None:
    body = {"data": {"conversations": [{"_id": "c1"}], "cursor": {"after": "http://evil.test/x"}}}
    router.get(CONVERSATIONS_URL).mock(return_value=json_response(body))
    assert (await api().list_conversations(ConversationQuery())).next_cursor is None


async def test_a_cursor_that_was_not_made_here_is_refused_before_any_request(
    router: respx.MockRouter,
) -> None:
    route = router.route().mock(return_value=json_response({}))
    for bad in ("garbage", "skip=3", "t.!!"):
        with pytest.raises(PMSValidationError):
            await api().list_conversations(ConversationQuery(cursor=bad))
        with pytest.raises(PMSValidationError):
            await api().get_conversation_messages(CONVERSATION_ID, PageRequest(cursor=bad))
    assert route.call_count == 0


@pytest.mark.parametrize(
    "body",
    [
        {"data": {}},
        {"data": {"conversations": "x"}},
        {"conversations": []},
        ["a"],
        {"data": []},
        {"data": {"conversations": [{"_id": f"c{n}"} for n in range(101)]}},
    ],
)
async def test_an_unreadable_list_is_an_upstream_error(
    router: respx.MockRouter, body: object
) -> None:
    router.get(CONVERSATIONS_URL).mock(return_value=json_response(body))
    with pytest.raises(PMSUpstreamError):
        await api().list_conversations(ConversationQuery())


async def test_items_without_an_id_are_skipped(router: respx.MockRouter) -> None:
    body = {"data": {"conversations": [{"meta": {}}, "text", {"_id": "c2"}]}}
    router.get(CONVERSATIONS_URL).mock(return_value=json_response(body))
    page = await api().list_conversations(ConversationQuery())
    assert [c.id for c in page.items] == ["c2"]


async def test_list_error_text_is_not_passed_on(router: respx.MockRouter) -> None:
    router.get(CONVERSATIONS_URL).mock(return_value=json_response({"message": "private text"}, 400))
    with pytest.raises(PMSValidationError) as caught:
        await api().list_conversations(ConversationQuery())
    assert "private text" not in str(caught.value)


# ---- get_conversation_messages ----


async def test_messages_are_read_newest_first_and_mapped(router: respx.MockRouter) -> None:
    route = router.get(POSTS_URL).mock(return_value=json_response(fixture("posts_page.json")))
    page = await api().get_conversation_messages(CONVERSATION_ID, PageRequest())
    assert query_of(route) == {"sort": "-createdAt"}
    assert [m.id[-2:] for m in page.items] == ["01", "02", "03", "04", "05"]
    assert [m.direction for m in page.items] == [
        MessageDirection.INBOUND,
        MessageDirection.OUTBOUND,
        MessageDirection.OUTBOUND,
        MessageDirection.SYSTEM,
        MessageDirection.INBOUND,
    ]
    assert page.next_cursor == encode_token_cursor("ZmFrZS1wb3N0LWN1cnNvcg")
    shown = page.model_dump_json()
    for private in NEVER_SHOWN:
        assert private not in shown
    await api().get_conversation_messages(CONVERSATION_ID, PageRequest(cursor=page.next_cursor))
    assert query_of(route)["cursorAfter"] == "ZmFrZS1wb3N0LWN1cnNvcg"


async def test_messages_use_the_requested_conversation_and_quote_its_id(
    router: respx.MockRouter,
) -> None:
    route = router.get(url__startswith=f"{CONVERSATIONS_URL}/").mock(
        return_value=json_response({"data": {"posts": [{"_id": "p1"}]}})
    )
    page = await api().get_conversation_messages(" a/b?c d ", PageRequest())
    assert route.calls.last.request.url.raw_path.startswith(
        b"/v1/communication/conversations/a%2Fb%3Fc%20d/posts"
    )
    assert page.items[0].conversation_id == "a/b?c d"


async def test_an_empty_or_over_long_conversation_id_is_refused(router: respx.MockRouter) -> None:
    route = router.route().mock(return_value=json_response({}))
    for bad in ("", "  ", "x" * 201):
        with pytest.raises(PMSValidationError):
            await api().get_conversation_messages(bad, PageRequest())
    assert route.call_count == 0


async def test_a_missing_conversation_is_not_found(router: respx.MockRouter) -> None:
    router.get(POSTS_URL).mock(return_value=json_response({"message": "private text"}, 404))
    with pytest.raises(PMSNotFoundError) as caught:
        await api().get_conversation_messages(CONVERSATION_ID, PageRequest())
    assert "private text" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        {"data": {}},
        {"data": {"posts": {}}},
        {"posts": []},
        {"data": {"posts": [{"_id": f"p{n}"} for n in range(101)]}},
    ],
)
async def test_unreadable_messages_are_an_upstream_error(
    router: respx.MockRouter, body: object
) -> None:
    router.get(POSTS_URL).mock(return_value=json_response(body))
    with pytest.raises(PMSUpstreamError):
        await api().get_conversation_messages(CONVERSATION_ID, PageRequest())


async def test_messages_without_an_id_are_skipped(router: respx.MockRouter) -> None:
    body = {"data": {"posts": [{"body": "no id"}, 7, {"_id": "p2", "body": "ok"}]}}
    router.get(POSTS_URL).mock(return_value=json_response(body))
    page = await api().get_conversation_messages(CONVERSATION_ID, PageRequest())
    assert [m.id for m in page.items] == ["p2"]


# ---- send_guest_message ----


def mock_posts(router: respx.MockRouter, types: list[str]) -> respx.Route:
    posts = [{"_id": f"p{n}", "module": {"type": kind}} for n, kind in enumerate(types)]
    return router.get(POSTS_URL).mock(return_value=json_response({"data": {"posts": posts}}))


async def test_the_default_channel_is_the_newest_guest_facing_one(router: respx.MockRouter) -> None:
    reads = router.get(POSTS_URL).mock(return_value=json_response(fixture("posts_page.json")))
    sends = router.post(SEND_URL).mock(return_value=json_response(fixture("send_response.json")))
    sent = await api().send_guest_message(outgoing("Welcome! Check-in is from 3 pm."))
    assert sends.call_count == 1 and reads.call_count == 1
    assert json.loads(sends.calls.last.request.content) == {
        "module": {"type": "airbnb2"},
        "body": "Welcome! Check-in is from 3 pm.",
    }
    assert sent.id.endswith("f10") and sent.direction is MessageDirection.OUTBOUND
    assert sent.channel == "airbnb2" and sent.author_name == "Riley Staffer"
    assert sent.conversation_id == CONVERSATION_ID
    assert sent.body == "Welcome! Check-in is from 3 pm."


async def test_a_note_or_log_is_never_used_as_the_channel(router: respx.MockRouter) -> None:
    mock_posts(router, ["note", "log", "sms", "email"])
    sends = router.post(SEND_URL).mock(return_value=json_response(fixture("send_response.json")))
    await api().send_guest_message(outgoing())
    assert json.loads(sends.calls.last.request.content)["module"] == {"type": "sms"}


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        (MessageChannel.EMAIL, "email"),
        (MessageChannel.SMS, "sms"),
        (MessageChannel.PLATFORM, "platform"),
        (MessageChannel.WHATSAPP, "whatsapp"),
    ],
)
async def test_a_channel_the_conversation_has_used_is_accepted(
    router: respx.MockRouter, channel: MessageChannel, expected: str
) -> None:
    mock_posts(router, ["airbnb2", "email", "sms", "platform", "whatsapp"])
    sends = router.post(SEND_URL).mock(return_value=json_response(fixture("send_response.json")))
    await api().send_guest_message(outgoing(channel=channel))
    assert json.loads(sends.calls.last.request.content)["module"] == {"type": expected}
    assert sends.call_count == 1


async def test_a_channel_the_conversation_has_not_used_is_refused_and_nothing_is_sent(
    router: respx.MockRouter,
) -> None:
    mock_posts(router, ["airbnb2", "note"])
    sends = router.post(SEND_URL).mock(return_value=json_response(fixture("send_response.json")))
    with pytest.raises(PMSValidationError) as caught:
        await api().send_guest_message(outgoing(channel=MessageChannel.SMS))
    assert "'sms' is not available" in str(caught.value) and "airbnb2" in str(caught.value)
    assert "Welcome" not in str(caught.value)
    assert sends.call_count == 0


async def test_a_conversation_without_earlier_messages_cannot_be_answered_by_default(
    router: respx.MockRouter,
) -> None:
    mock_posts(router, ["note", "log"])
    sends = router.post(SEND_URL).mock(return_value=json_response(fixture("send_response.json")))
    with pytest.raises(PMSValidationError, match="cannot be told"):
        await api().send_guest_message(outgoing())
    with pytest.raises(PMSValidationError, match="none yet"):
        await api().send_guest_message(outgoing(channel=MessageChannel.EMAIL))
    assert sends.call_count == 0


async def test_if_the_channel_check_fails_nothing_is_sent(router: respx.MockRouter) -> None:
    router.get(POSTS_URL).mock(return_value=json_response({"message": "x"}, 404))
    sends = router.post(SEND_URL).mock(return_value=json_response(fixture("send_response.json")))
    with pytest.raises(PMSNotFoundError):
        await api().send_guest_message(outgoing())
    assert sends.call_count == 0


async def test_the_answer_may_be_the_post_itself(router: respx.MockRouter) -> None:
    mock_posts(router, ["email"])
    post = {"_id": "p9", "body": "hi", "module": {"type": "email"}, "sentBy": "guest"}
    router.post(SEND_URL).mock(return_value=json_response({"status": 200, "data": post}))
    sent = await api().send_guest_message(outgoing("hi"))
    assert sent.id == "p9" and sent.direction is MessageDirection.OUTBOUND  # we sent it


@pytest.mark.parametrize("answer", [{}, {"data": {}}, {"data": {"post": {}}}, ["x"], None])
async def test_an_answer_that_cannot_be_read_is_reported_as_probably_sent(
    router: respx.MockRouter, answer: object
) -> None:
    mock_posts(router, ["email"])
    sends = router.post(SEND_URL).mock(return_value=json_response(answer))
    with pytest.raises(PMSUpstreamError, match="probably sent"):
        await api().send_guest_message(outgoing())
    assert sends.call_count == 1


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("slow"), httpx.ReadError("dropped"), httpx.WriteTimeout("w")]
)
async def test_a_timeout_or_drop_is_never_resent_and_the_outcome_is_unknown(
    router: respx.MockRouter, failure: Exception
) -> None:
    mock_posts(router, ["email"])
    sends = router.post(SEND_URL).mock(side_effect=failure)
    with pytest.raises(PMSUpstreamError, match="outcome is unknown"):
        await api().send_guest_message(outgoing())
    assert sends.call_count == 1


async def test_a_server_error_is_never_resent(router: respx.MockRouter) -> None:
    mock_posts(router, ["email"])
    sends = router.post(SEND_URL).mock(return_value=json_response({"message": "private"}, 503))
    with pytest.raises(PMSUpstreamError) as caught:
        await api().send_guest_message(outgoing())
    assert sends.call_count == 1 and "private" not in str(caught.value)


async def test_a_rejection_is_reported_without_upstream_text(router: respx.MockRouter) -> None:
    mock_posts(router, ["email"])
    router.post(SEND_URL).mock(return_value=json_response({"message": "private"}, 400))
    with pytest.raises(PMSValidationError) as caught:
        await api().send_guest_message(outgoing("secret draft text"))
    assert "private" not in str(caught.value) and "secret draft" not in str(caught.value)


async def test_a_rate_limit_without_a_wait_time_is_not_resent(router: respx.MockRouter) -> None:
    mock_posts(router, ["email"])
    sends = router.post(SEND_URL).mock(return_value=json_response({}, 429))
    with pytest.raises(base.PMSRateLimitedError):
        await api().send_guest_message(outgoing())
    assert sends.call_count == 1


async def test_no_message_is_sent_when_the_deadline_is_too_close(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_posts(router, ["email"])
    sends = router.post(SEND_URL).mock(return_value=json_response(fixture("send_response.json")))
    monkeypatch.setattr(inbox, "remaining_seconds", lambda: 0.5)
    with pytest.raises(PMSUpstreamError, match="Nothing was sent"):
        await api().send_guest_message(outgoing())
    assert sends.call_count == 0


# ---- notes ----


async def test_adding_a_note_is_refused_and_nothing_is_requested(router: respx.MockRouter) -> None:
    route = router.route().mock(return_value=json_response({}))
    with pytest.raises(CapabilityNotSupportedError):
        await api().add_conversation_note(ConversationNote(conversation_id="c1", body="x"))
    assert route.call_count == 0


# ---- logs ----


async def test_logs_never_hold_message_text_or_guest_details(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    router.get(CONVERSATIONS_URL).mock(
        return_value=json_response(
            {
                "data": {
                    "conversations": [
                        {"meta": {"guest": {"fullName": GUEST_NAME, "email": GUEST_EMAIL}}},
                        {"_id": "c9", "meta": {"guest": {"fullName": GUEST_NAME}}},
                    ]
                }
            }
        )
    )
    await api().list_conversations(ConversationQuery())
    router.get(POSTS_URL).mock(
        return_value=json_response(
            {"data": {"posts": [{"body": INJECTION_TEXT}, {"_id": "p1", "body": INJECTION_TEXT}]}}
        )
    )
    await api().get_conversation_messages(CONVERSATION_ID, PageRequest())
    mock_posts(router, ["platform"])
    router.post(SEND_URL).mock(return_value=json_response({"message": INJECTION_TEXT}, 500))
    with pytest.raises(PMSUpstreamError):
        await api().send_guest_message(outgoing(INJECTION_TEXT, MessageChannel.PLATFORM))
    assert caplog.records
    recorded = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for private in (GUEST_NAME, GUEST_EMAIL, INJECTION_TEXT):
        assert private not in recorded


# ---- capabilities ----


def test_capabilities_and_wiring() -> None:
    assert Capability.INBOX_READ in inbox.CAPABILITIES
    assert Capability.INBOX_SEND in inbox.CAPABILITIES and len(inbox.CAPABILITIES) == 2
    assert Capability.INBOX_NOTE not in inbox.CAPABILITIES
    assert DOMAIN_MODULES["inbox"] == "inbox"
    assert "inbox" in discover_domains()


# ---- channel and last message (requested with `fields`) ----


def test_a_conversation_gives_its_channel_and_last_message_time() -> None:
    found = inbox.map_conversation(
        {
            "_id": "6a5000000000000000000c01",
            "meta": {
                "integration": {"platform": "airbnb2", "status": "DISCONNECTED"},
                "guest": {"_id": "6a3000000000000000000001", "fullName": "Fake Guest"},
                "reservations": [],
            },
            "state": {
                "read": True,
                "status": "OPEN",
                "lastMessage": {"body": "Fake text", "date": "2026-11-14T16:20:54.320Z"},
            },
            "createdAt": "2026-11-14T16:18:57.605Z",
        }
    )
    assert found is not None
    assert found.channel == "airbnb2"
    assert found.last_message_at is not None
    assert found.last_message_at.isoformat().startswith("2026-11-14T16:20:54")
    assert found.unread is False
    assert "Fake text" not in found.model_dump_json()  # the preview text is not passed on
