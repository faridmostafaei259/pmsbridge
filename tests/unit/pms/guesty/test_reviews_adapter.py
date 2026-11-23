"""The Guesty reviews adapter through the real client, with respx as the only network."""

import json
import logging
from collections.abc import AsyncIterator
from datetime import date

import httpx
import pytest
import respx
from guesty_review_helpers import (
    DRAFT_TEXT,
    FAILED_REPLY_ID,
    GUEST_NAME,
    INJECTION_TEXT,
    REPLIED_ID,
    REPLY_URL,
    REVIEW_ID,
    REVIEWS_URL,
    fixture,
    json_response,
    make_client,
    mock_router,
)

from pmsbridge.pms import base
from pmsbridge.pms.base import (
    Capability,
    PMSNotFoundError,
    PMSUpstreamError,
    PMSValidationError,
)
from pmsbridge.pms.guesty import reviews
from pmsbridge.pms.guesty.common import encode_cursor
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains
from pmsbridge.pms.models import ReviewQuery, ReviewReply


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        yield mocked


def query_of(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


def api() -> reviews.GuestyReviews:
    return reviews.build(make_client())


def reply_of(review_id: str = REVIEW_ID, body: str = "Thank you!") -> ReviewReply:
    return ReviewReply(review_id=review_id, body=body)


def mock_list(router: respx.MockRouter) -> respx.Route:
    return router.get(REVIEWS_URL).mock(return_value=json_response(fixture("list_page.json")))


# ---- list_reviews ----


async def test_list_sends_the_default_parameters(router: respx.MockRouter) -> None:
    route = router.get(REVIEWS_URL).mock(return_value=json_response({"data": []}))
    page = await api().list_reviews(ReviewQuery(limit=10))
    assert page.items == [] and page.next_cursor is None and page.total is None
    assert query_of(route) == {"includeCustomChannels": "true", "limit": "10", "skip": "0"}


async def test_list_sends_the_filters(router: respx.MockRouter) -> None:
    route = router.get(REVIEWS_URL).mock(return_value=json_response({"data": []}))
    await api().list_reviews(ReviewQuery(listing_id="L1", since=date(2026, 3, 1)))
    params = query_of(route)
    assert params["listingId"] == "L1" and params["startDate"] == "2026-03-01T00:00:00.000Z"


async def test_list_maps_the_fixture_and_pages_with_a_skip_cursor(router: respx.MockRouter) -> None:
    route = mock_list(router)
    page = await api().list_reviews(ReviewQuery(limit=3))
    assert [r.id for r in page.items] == [
        REVIEW_ID,
        REPLIED_ID,
        FAILED_REPLY_ID,
    ]
    assert page.next_cursor == encode_cursor(3)
    await api().list_reviews(ReviewQuery(limit=3, cursor=page.next_cursor))
    assert query_of(route)["skip"] == "3"


@pytest.mark.parametrize(
    "answer",
    [
        {"data": [{"_id": "a"}]},
        {"data": {"results": [{"_id": "a"}]}},
        {"results": [{"_id": "a"}]},
        [{"_id": "a"}],
    ],
)
async def test_every_documented_shape_of_the_list_is_read(
    router: respx.MockRouter, answer: object
) -> None:
    router.get(REVIEWS_URL).mock(return_value=json_response(answer))
    page = await api().list_reviews(ReviewQuery())
    assert [r.id for r in page.items] == ["a"] and page.next_cursor is None


async def test_the_list_ends_on_a_short_page_and_follows_a_smaller_page_size(
    router: respx.MockRouter,
) -> None:
    two = [{"_id": "a"}, {"_id": "b"}]
    router.get(REVIEWS_URL).mock(return_value=json_response({"data": two, "limit": 2}))
    page = await api().list_reviews(ReviewQuery(limit=50))
    assert page.next_cursor == encode_cursor(2)  # Guesty used a page of 2: this one was full
    router.get(REVIEWS_URL).mock(return_value=json_response({"data": two, "limit": 50}))
    assert (await api().list_reviews(ReviewQuery(limit=50))).next_cursor is None


@pytest.mark.parametrize("answer", [{"data": "x"}, {"data": {"results": 3}}, "text", {}])
async def test_a_list_that_cannot_be_understood_is_an_upstream_error(
    router: respx.MockRouter, answer: object
) -> None:
    router.get(REVIEWS_URL).mock(return_value=json_response(answer))
    with pytest.raises(PMSUpstreamError, match="could not be understood"):
        await api().list_reviews(ReviewQuery())


async def test_more_reviews_than_a_page_is_an_upstream_error(router: respx.MockRouter) -> None:
    many = [{"_id": str(n)} for n in range(101)]
    router.get(REVIEWS_URL).mock(return_value=json_response({"data": many}))
    with pytest.raises(PMSUpstreamError, match="more reviews than expected"):
        await api().list_reviews(ReviewQuery())


async def test_an_unusable_item_is_skipped_and_the_rest_are_kept(router: respx.MockRouter) -> None:
    router.get(REVIEWS_URL).mock(return_value=json_response({"data": [{"x": 1}, {"_id": "ok"}]}))
    page = await api().list_reviews(ReviewQuery())
    assert [r.id for r in page.items] == ["ok"]


async def test_a_bad_cursor_is_refused_before_any_request(router: respx.MockRouter) -> None:
    route = router.get(REVIEWS_URL).mock(return_value=json_response({"data": []}))
    with pytest.raises(PMSValidationError, match="cursor"):
        await api().list_reviews(ReviewQuery(cursor="not-a-cursor"))
    assert route.call_count == 0


async def test_the_list_is_read_again_after_a_temporary_failure(router: respx.MockRouter) -> None:
    route = router.get(REVIEWS_URL).mock(
        side_effect=[httpx.Response(503), json_response({"data": [{"_id": "a"}]})]
    )
    page = await api().list_reviews(ReviewQuery())
    assert [r.id for r in page.items] == ["a"] and route.call_count == 2


# ---- reply_to_review ----


async def test_a_reply_is_one_put_after_finding_the_review(router: respx.MockRouter) -> None:
    mock_list(router)
    put = router.put(REPLY_URL).mock(return_value=json_response(fixture("reply_response.json")))
    sent = await api().reply_to_review(reply_of(body="Thank you, we hope to host you again."))
    assert put.call_count == 1
    assert json.loads(put.calls.last.request.content) == {
        "reviewReply": "Thank you, we hope to host you again."
    }
    assert sent.id == REVIEW_ID and sent.reply == "Thank you, we hope to host you again."
    assert sent.extras["reply_status"] == "PENDING"


async def test_a_review_that_already_has_a_reply_is_refused_and_nothing_is_sent(
    router: respx.MockRouter,
) -> None:
    mock_list(router)
    put = router.route(method="PUT").mock(return_value=json_response({}))
    with pytest.raises(PMSValidationError, match="already has a reply"):
        await api().reply_to_review(reply_of(REPLIED_ID))
    assert put.call_count == 0


async def test_a_failed_earlier_reply_does_not_block_a_new_one(router: respx.MockRouter) -> None:
    mock_list(router)
    put = router.put(f"{REVIEWS_URL}/{FAILED_REPLY_ID}/reply").mock(
        return_value=json_response({"_id": FAILED_REPLY_ID})
    )
    sent = await api().reply_to_review(reply_of(FAILED_REPLY_ID))
    assert put.call_count == 1 and sent.id == FAILED_REPLY_ID


async def test_a_review_that_is_not_found_is_refused_and_nothing_is_sent(
    router: respx.MockRouter,
) -> None:
    mock_list(router)
    put = router.route(method="PUT").mock(return_value=json_response({}))
    with pytest.raises(PMSNotFoundError, match="nothing was sent"):
        await api().reply_to_review(reply_of("missing"))
    assert put.call_count == 0


async def test_the_search_for_the_review_pages_and_stops(router: respx.MockRouter) -> None:
    full = [{"_id": f"x{n}"} for n in range(100)]
    route = router.get(REVIEWS_URL).mock(
        side_effect=[
            json_response({"data": full}),
            json_response({"data": [{"_id": "target"}]}),
        ]
    )
    put = router.put(f"{REVIEWS_URL}/target/reply").mock(
        return_value=json_response({"_id": "target"})
    )
    await api().reply_to_review(reply_of("target"))
    assert [dict(c.request.url.params)["skip"] for c in route.calls] == ["0", "100"]
    assert put.call_count == 1


async def test_the_search_is_bounded(router: respx.MockRouter) -> None:
    full = [{"_id": f"x{n}"} for n in range(100)]
    route = router.get(REVIEWS_URL).mock(return_value=json_response({"data": full}))
    put = router.route(method="PUT").mock(return_value=json_response({}))
    with pytest.raises(PMSNotFoundError):
        await api().reply_to_review(reply_of("target"))
    assert route.call_count == reviews.MAX_SCAN_PAGES and put.call_count == 0


@pytest.mark.parametrize("review_id", ["  ", "x" * 201])
async def test_a_bad_id_is_refused_before_any_request(
    router: respx.MockRouter, review_id: str
) -> None:
    route = router.route().mock(return_value=json_response({}))
    with pytest.raises(PMSValidationError):
        await api().reply_to_review(ReviewReply.model_construct(review_id=review_id, body="Hi"))
    assert route.call_count == 0


async def test_the_id_in_the_path_cannot_add_segments(router: respx.MockRouter) -> None:
    router.get(REVIEWS_URL).mock(return_value=json_response({"data": [{"_id": "a/b"}]}))
    put = router.put(f"{REVIEWS_URL}/a%2Fb/reply").mock(return_value=json_response({"_id": "a/b"}))
    await api().reply_to_review(reply_of("a/b"))
    assert put.call_count == 1


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("slow"), httpx.ReadError("dropped"), httpx.WriteTimeout("w")]
)
async def test_a_timeout_or_drop_is_never_resent_and_the_outcome_is_unknown(
    router: respx.MockRouter, failure: Exception
) -> None:
    mock_list(router)
    put = router.put(REPLY_URL).mock(side_effect=failure)
    with pytest.raises(PMSUpstreamError, match="outcome is unknown"):
        await api().reply_to_review(reply_of())
    assert put.call_count == 1


async def test_a_server_error_is_never_resent(router: respx.MockRouter) -> None:
    mock_list(router)
    put = router.put(REPLY_URL).mock(return_value=json_response({"message": "private"}, 503))
    with pytest.raises(PMSUpstreamError) as caught:
        await api().reply_to_review(reply_of())
    assert put.call_count == 1 and "private" not in str(caught.value)


async def test_a_rejection_is_reported_without_upstream_text_or_the_draft(
    router: respx.MockRouter,
) -> None:
    mock_list(router)
    router.put(REPLY_URL).mock(return_value=json_response({"message": "private"}, 400))
    with pytest.raises(PMSValidationError) as caught:
        await api().reply_to_review(reply_of(body=DRAFT_TEXT))
    assert "private" not in str(caught.value) and DRAFT_TEXT not in str(caught.value)


async def test_a_rate_limit_without_a_wait_time_is_not_resent(router: respx.MockRouter) -> None:
    mock_list(router)
    put = router.put(REPLY_URL).mock(return_value=json_response({}, 429))
    with pytest.raises(base.PMSRateLimitedError):
        await api().reply_to_review(reply_of())
    assert put.call_count == 1


@pytest.mark.parametrize("answer", [[], "text", {}, {"data": "x"}, {"_id": ""}])
async def test_an_answer_that_cannot_be_understood_says_it_was_probably_sent(
    router: respx.MockRouter, answer: object
) -> None:
    mock_list(router)
    put = router.put(REPLY_URL).mock(return_value=json_response(answer))
    with pytest.raises(PMSUpstreamError, match="probably sent"):
        await api().reply_to_review(reply_of())
    assert put.call_count == 1


async def test_a_body_that_is_not_json_says_it_was_probably_sent(router: respx.MockRouter) -> None:
    mock_list(router)
    router.put(REPLY_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(PMSUpstreamError, match="probably sent"):
        await api().reply_to_review(reply_of())


async def test_no_reply_is_sent_when_the_deadline_is_too_close(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_list(router)
    put = router.put(REPLY_URL).mock(return_value=json_response(fixture("reply_response.json")))
    monkeypatch.setattr(reviews, "remaining_seconds", lambda: 0.5)
    with pytest.raises(PMSUpstreamError, match="Nothing was sent"):
        await api().reply_to_review(reply_of())
    assert put.call_count == 0


# ---- logs ----


async def test_logs_never_hold_review_text_reply_text_or_guest_names(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    router.get(REVIEWS_URL).mock(
        return_value=json_response(
            {
                "data": [
                    {"contents": {"body": INJECTION_TEXT, "reviewerName": GUEST_NAME}},
                    {"_id": REVIEW_ID, "contents": {"body": INJECTION_TEXT}},
                ]
            }
        )
    )
    await api().list_reviews(ReviewQuery())
    router.put(REPLY_URL).mock(return_value=json_response({"message": DRAFT_TEXT}, 500))
    with pytest.raises(PMSUpstreamError):
        await api().reply_to_review(reply_of(body=DRAFT_TEXT))
    assert caplog.records
    recorded = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for private in (GUEST_NAME, INJECTION_TEXT, DRAFT_TEXT):
        assert private not in recorded


# ---- capabilities ----


def test_capabilities_and_wiring() -> None:
    assert {Capability.REVIEWS_READ, Capability.REVIEWS_REPLY} == reviews.CAPABILITIES
    assert DOMAIN_MODULES["reviews"] == "reviews"
    assert "reviews" in discover_domains()


# ---- Airbnb reviews (`rawReview`) ----

AIRBNB_ID = "6a7000000000000000000a01"


def airbnb_review(**raw: object) -> dict[str, object]:
    """An Airbnb review as Guesty lists it: the review itself is inside `rawReview`."""
    return {
        "_id": AIRBNB_ID,
        "channelId": "airbnb2",
        "listingId": "6a1000000000000000000001",
        "reservationId": "6a2000000000000000000001",
        "createdAt": "2026-11-07T23:52:48.939Z",
        "reviewReplies": [],
        "rawReview": {
            "reviewer_role": "guest",
            "overall_rating": 5,
            "public_review": "A lovely flat, fake review text.",
            "private_feedback": "Fake private note to the host.",
            **raw,
        },
    }


def test_an_airbnb_review_is_read_from_raw_review() -> None:
    found = reviews.map_review(airbnb_review(reviewee_response="Fake thank-you reply."))
    assert found is not None
    assert found.rating == 5.0 and found.rating_scale == 5.0
    assert found.public_text == "A lovely flat, fake review text."
    assert found.private_text == "Fake private note to the host."
    assert found.reply == "Fake thank-you reply."
    assert found.channel == "airbnb2"


def test_an_airbnb_review_without_a_response_has_no_reply() -> None:
    found = reviews.map_review(airbnb_review())
    assert found is not None and found.reply is None


async def test_an_airbnb_review_answered_on_airbnb_is_refused_and_nothing_is_sent(
    router: respx.MockRouter,
) -> None:
    router.get(REVIEWS_URL).mock(
        return_value=json_response(
            {"data": [airbnb_review(reviewee_response="Fake earlier reply.")], "limit": 100}
        )
    )
    put = router.route(method="PUT").mock(return_value=json_response({}))
    with pytest.raises(PMSValidationError, match="already has a reply"):
        await api().reply_to_review(reply_of(AIRBNB_ID))
    assert put.call_count == 0
