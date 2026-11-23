"""Mapping Guesty conversations and posts to the canonical models, and the paging-token cursor."""

from datetime import UTC, datetime

import pytest
from guesty_inbox_helpers import (
    CONVERSATION_ID,
    GUEST_EMAIL,
    GUEST_NAME,
    NEVER_SHOWN,
    STAFF_NAME,
    fixture,
)

from pmsbridge.pms.base import PMSValidationError
from pmsbridge.pms.guesty.common import decode_token_cursor, encode_token_cursor
from pmsbridge.pms.guesty.inbox import map_conversation, map_post
from pmsbridge.pms.models import MessageDirection


def conversations() -> list[object]:
    body = fixture("conversations_page.json")
    assert isinstance(body, dict)
    return body["data"]["conversations"]  # type: ignore[no-any-return]


def posts() -> list[object]:
    body = fixture("posts_page.json")
    assert isinstance(body, dict)
    return body["data"]["posts"]  # type: ignore[no-any-return]


def test_a_full_conversation_is_mapped() -> None:
    found = map_conversation(conversations()[0])
    assert found is not None
    assert found.id == CONVERSATION_ID
    assert found.reservation_id == "6a4000000000000000000b01"
    assert found.listing_id == "6a1000000000000000000d01"
    assert found.guest_id == "6a3000000000000000000a01" and found.guest_name == GUEST_NAME
    assert found.unread is True
    assert found.last_message_at == datetime(2026, 3, 5, 9, 30, tzinfo=UTC)
    assert found.channel is None and found.available_channels == []
    assert found.extras == {
        "status": "OPEN",
        "reservation_status": "confirmed",
        "confirmation_code": "INV-1234",
        "check_in": "2026-04-01T15:00:00.000Z",
        "check_out": "2026-04-05T11:00:00.000Z",
        "listing_title": "Invented Beach House",
        "returning_guest": True,
        "assignee_id": "6a5000000000000000000e01",
        "priority": 2,
    }
    shown = found.model_dump_json()
    for private in NEVER_SHOWN:
        assert private not in shown


def test_the_last_message_time_falls_back_to_the_newest_of_the_two_senders() -> None:
    found = map_conversation(conversations()[1])
    assert found is not None
    assert found.last_message_at == datetime(2026, 3, 4, 8, 0, tzinfo=UTC)
    assert found.unread is False
    assert found.reservation_id == "6a4000000000000000000b02"
    assert found.listing_id == "6a1000000000000000000d02"
    assert found.extras["reservation_count"] == 2


def test_a_conversation_with_only_an_id_is_mapped_without_inventing_anything() -> None:
    found = map_conversation(conversations()[2])
    assert found is not None
    assert found.guest_name is None and found.unread is None and found.last_message_at is None
    assert found.reservation_id is None and found.extras == {}


@pytest.mark.parametrize("bad", [None, "text", [], {}, {"_id": "  "}, {"_id": 5}, {"id": "x"}])
def test_a_conversation_without_a_usable_id_is_skipped(bad: object) -> None:
    assert map_conversation(bad) is None


def test_wrong_typed_values_are_ignored() -> None:
    found = map_conversation(
        {
            "_id": "c1",
            "state": {"read": "no", "status": 3, "lastMessage": {"date": "2026-03-05"}},
            "meta": {"guest": "x", "reservations": ["y", {"_id": 4, "listing": []}]},
            "priority": True,
        }
    )
    assert found is not None
    assert found.unread is None and found.last_message_at is None and found.guest_id is None
    assert found.reservation_id is None and found.extras == {}


def test_a_guest_post_is_inbound_unnamed_and_keeps_its_text() -> None:
    found = map_post(posts()[0], CONVERSATION_ID)
    assert found is not None
    assert found.direction is MessageDirection.INBOUND
    assert found.author_name is None  # the guest's name is never added
    assert found.channel == "airbnb2" and "ignore previous instructions" in found.body
    assert found.sent_at == datetime(2026, 3, 5, 9, 30, tzinfo=UTC)
    assert found.conversation_id == CONVERSATION_ID
    shown = found.model_dump_json()
    assert "INVENTED-ATTACHMENT" not in shown and GUEST_EMAIL not in shown


def test_a_staff_post_is_outbound_and_named() -> None:
    found = map_post(posts()[1], CONVERSATION_ID)
    assert found is not None
    assert found.direction is MessageDirection.OUTBOUND and found.author_name == STAFF_NAME
    assert GUEST_EMAIL not in found.model_dump_json()  # `module.to` is never read


def test_a_note_is_outbound_on_the_note_channel() -> None:
    found = map_post(posts()[2], CONVERSATION_ID)
    assert found is not None
    assert found.direction is MessageDirection.OUTBOUND and found.channel == "note"


def test_a_log_entry_is_a_system_message() -> None:
    found = map_post(posts()[3], CONVERSATION_ID)
    assert found is not None
    assert found.direction is MessageDirection.SYSTEM and found.channel == "log"
    assert found.author_name is None


def test_a_post_that_does_not_say_who_wrote_it_counts_as_inbound() -> None:
    found = map_post(posts()[4], CONVERSATION_ID)
    assert found is not None
    assert found.direction is MessageDirection.INBOUND and found.author_name is None


@pytest.mark.parametrize(
    ("post", "expected"),
    [
        ({"sentBy": "guest", "from": {"type": "user"}}, MessageDirection.INBOUND),
        ({"sentBy": "host"}, MessageDirection.OUTBOUND),
        ({"from": {"type": "user"}}, MessageDirection.OUTBOUND),
        ({"sentBy": "log"}, MessageDirection.SYSTEM),
        ({"sentBy": "robot"}, MessageDirection.INBOUND),
        ({}, MessageDirection.INBOUND),
    ],
)
def test_direction_rules(post: dict[str, object], expected: MessageDirection) -> None:
    found = map_post({"_id": "p1", **post}, "c1")
    assert found is not None and found.direction is expected


def test_a_very_long_body_is_shortened_and_a_missing_one_is_empty() -> None:
    long = map_post({"_id": "p1", "body": "x" * 9000}, "c1")
    assert long is not None and len(long.body) == 5000 and long.body.endswith("[truncated]")
    none = map_post({"_id": "p2", "body": 5}, "c1")
    assert none is not None and none.body == ""


@pytest.mark.parametrize("bad", [None, "text", [], {}, {"_id": ""}, {"_id": 5}])
def test_a_post_without_a_usable_id_is_skipped(bad: object) -> None:
    assert map_post(bad, "c1") is None


def test_a_bad_time_is_left_empty() -> None:
    found = map_post({"_id": "p1", "createdAt": "yesterday"}, "c1")
    assert found is not None and found.sent_at is None


# ---- paging-token cursor ----


def test_a_token_survives_a_round_trip() -> None:
    cursor = encode_token_cursor("abc.DEF-123_xyz==")
    assert cursor is not None and cursor.startswith("t.")
    assert decode_token_cursor(cursor) == "abc.DEF-123_xyz=="
    assert decode_token_cursor(None) is None


@pytest.mark.parametrize(
    "token", [None, "", "  ", 5, "has space", "x" * 501, "http://evil.example/x", "tab\tx", "é"]
)
def test_an_unusable_token_makes_no_cursor(token: object) -> None:
    assert encode_token_cursor(token) is None


@pytest.mark.parametrize(
    "cursor",
    ["", "garbage", "skip=5", "t.", "t.!!!", "t.YSBi", "t." + "A" * 2000, "t.aHR0cDovL2V2aWwvLy8"],
)
def test_a_cursor_that_was_not_made_here_is_refused(cursor: str) -> None:
    with pytest.raises(PMSValidationError):
        decode_token_cursor(cursor)


def test_the_cursor_holds_no_readable_url() -> None:
    cursor = encode_token_cursor("fake-cursor-1")
    assert cursor is not None and "fake-cursor" not in cursor
