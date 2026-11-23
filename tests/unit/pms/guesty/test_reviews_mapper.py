"""Mapping Guesty reviews to the canonical `Review`."""

import logging

import pytest
from guesty_review_helpers import GUEST_NAME, INJECTION_TEXT, NEVER_SHOWN, fixture

from pmsbridge.pms.guesty import reviews


def items() -> list[object]:
    data = fixture("list_page.json")
    assert isinstance(data, dict)
    found = data["data"]
    assert isinstance(found, list)
    return found


def test_a_custom_channel_review_keeps_text_rating_and_scale() -> None:
    review = reviews.map_review(items()[0])
    assert review is not None
    assert review.id == "6a7000000000000000000r01"
    assert review.listing_id == "6a1000000000000000000a01"
    assert review.reservation_id == "6a2000000000000000000b01"
    assert review.channel == "custom"
    assert review.rating == 9.0 and review.rating_scale == 10.0
    assert review.public_text == INJECTION_TEXT
    assert review.private_text is None and review.reply is None
    assert review.created_at is not None and review.created_at.isoformat().startswith("2026-02-01")
    assert review.extras["channel_review_id"] == "ext-invented-1"
    for leaked in NEVER_SHOWN:
        assert leaked not in review.model_dump_json()


def test_a_channel_review_without_contents_has_no_text_or_rating() -> None:
    review = reviews.map_review(items()[1])
    assert review is not None
    assert review.channel == "airbnb2"
    assert review.rating is None and review.rating_scale is None and review.public_text is None
    assert review.created_at is not None and review.created_at.isoformat().startswith("2026-01-10")


def test_a_completed_reply_is_shown_with_its_status() -> None:
    review = reviews.map_review(items()[1])
    assert review is not None
    assert review.reply == "Thank you for staying with us."
    assert review.extras["reply_status"] == "COMPLETED"
    assert review.extras["replied_at"] == "2026-01-12T08:30:00.000Z"


def test_a_failed_reply_does_not_count_as_a_reply() -> None:
    review = reviews.map_review(items()[2])
    assert review is not None and review.reply is None
    assert "reply_status" not in review.extras


def test_a_pending_reply_with_no_text_is_still_a_reply_on_its_way() -> None:
    review = reviews.map_review({"_id": "r9", "reviewReplies": [{"status": "PENDING"}]})
    assert review is not None and review.reply is None
    assert review.extras["reply_status"] == "PENDING"


@pytest.mark.parametrize("rating", [-1, "9", True, None, float("inf")])
def test_an_unusable_rating_is_left_out_with_its_scale(rating: object) -> None:
    review = reviews.map_review({"_id": "r1", "contents": {"rating": rating}})
    assert review is not None and review.rating is None and review.rating_scale is None


def test_long_text_is_shortened_not_rejected() -> None:
    review = reviews.map_review({"_id": "r1", "contents": {"body": "x" * 9000}})
    assert review is not None and review.public_text is not None
    assert len(review.public_text) == 5000 and review.public_text.endswith("[truncated]")


@pytest.mark.parametrize("raw", [None, "text", [], {}, {"_id": ""}, {"_id": 5}, {"_id": "a" * 300}])
def test_an_item_without_a_usable_id_is_skipped_and_logged_without_detail(
    raw: object, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    assert reviews.map_review(raw) is None
    assert caplog.records


def test_a_review_wrapped_in_data_is_read() -> None:
    review = reviews.map_review({"data": {"_id": "r1", "channelId": "airbnb2"}})
    assert review is not None and review.id == "r1" and review.channel == "airbnb2"


def test_odd_value_types_do_not_raise() -> None:
    review = reviews.map_review(
        {
            "_id": "r1",
            "contents": "not an object",
            "reviewReplies": ["x", 5, {"status": 3, "reviewReply": ["a"]}],
            "createdAt": 5,
            "listingId": {"a": 1},
        }
    )
    assert review is not None and review.listing_id is None and review.created_at is None


def test_the_guest_name_is_never_read(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    review = reviews.map_review(items()[0])
    assert review is not None
    assert GUEST_NAME not in review.model_dump_json()
    assert GUEST_NAME not in " ".join(r.getMessage() for r in caplog.records)
