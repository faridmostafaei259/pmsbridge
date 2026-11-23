from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from pmsbridge.pms.base import WRITE_CAPABILITIES, Capability, PMSProvider
from pmsbridge.pms.models import (
    MAX_CHECKLIST_ITEMS,
    MAX_PHOTOS,
    MAX_TEXT_LENGTH,
    TRUNCATION_MARKER,
    Bed,
    ConversationNote,
    DescriptionKind,
    HouseRules,
    ListingDescription,
    ListingDetails,
    ListingPhoto,
    Money,
    NewTask,
    PhotoCaptionUpdate,
    PhotoOrder,
    PricingSettings,
    Room,
    Task,
    TaskQuery,
    TaskType,
)
from pmsbridge.pms.testing import FakeProvider


def test_listing_details_round_trip_and_truncation() -> None:
    details = ListingDetails(
        listing_id="l1",
        descriptions=[
            ListingDescription(kind=DescriptionKind.SUMMARY, text="Sea view flat"),
            ListingDescription(text="x" * (MAX_TEXT_LENGTH + 100)),
        ],
        amenities=["wifi", "pool"],
        house_rules=HouseRules(pets_allowed=False, minimum_age=21, quiet_hours_start="22:00"),
        rooms=[Room(name="Bedroom 1", beds=[Bed(bed_type="queen")])],
        custom_fields={"floor": 3},
    )
    assert details.descriptions[1].kind is DescriptionKind.OTHER
    assert details.descriptions[1].text.endswith(TRUNCATION_MARKER)
    assert ListingDetails.model_validate_json(details.model_dump_json()) == details


def test_house_rules_and_beds_are_bounded() -> None:
    with pytest.raises(ValidationError):
        HouseRules(minimum_age=200)
    with pytest.raises(ValidationError):
        Bed(bed_type="single", count=0)


def test_photo_inputs_are_validated() -> None:
    ListingPhoto(id="p1", url="https://images.example.test/p1.jpg", position=0)
    with pytest.raises(ValidationError):
        PhotoCaptionUpdate(listing_id="l1", photo_id="p1", caption="x" * 501)
    with pytest.raises(ValidationError):
        PhotoOrder(listing_id="l1", photo_ids=[])
    with pytest.raises(ValidationError):
        PhotoOrder(listing_id="l1", photo_ids=["p1", "p1"])
    with pytest.raises(ValidationError):
        PhotoOrder(listing_id="l1", photo_ids=[f"p{i}" for i in range(MAX_PHOTOS + 1)])
    assert PhotoOrder(listing_id="l1", photo_ids=["p2", "p1"]).photo_ids == ["p2", "p1"]


def test_pricing_settings_percentages_are_bounded() -> None:
    usd = Money(amount=Decimal("120"), currency="USD")
    PricingSettings(listing_id="l1", currency="USD", base_price=usd, weekly_discount_percent=10)
    with pytest.raises(ValidationError):
        PricingSettings(listing_id="l1", currency="USD", weekly_discount_percent=150)
    with pytest.raises(ValidationError):
        PricingSettings(listing_id="l1", currency="usd")


def test_conversation_note_needs_text() -> None:
    with pytest.raises(ValidationError):
        ConversationNote(conversation_id="c1", body="")
    assert (
        ConversationNote(conversation_id="c1", body="Called the guest").body == "Called the guest"
    )


def test_tasks_carry_a_type_and_checklist() -> None:
    task = Task(id="t1", title="Turnover clean", task_type=TaskType.CLEANING)
    assert task.checklist == []
    assert Task(id="t2", title="Fix tap").task_type is TaskType.UNKNOWN
    assert TaskQuery(task_type=TaskType.CLEANING).task_type is TaskType.CLEANING
    new = NewTask(title="Clean", task_type=TaskType.CLEANING, checklist=["Beds", "Towels"])
    assert new.checklist == ["Beds", "Towels"]
    assert NewTask(title="Other").task_type is TaskType.OTHER
    with pytest.raises(ValidationError):
        NewTask(title="Clean", checklist=["x"] * (MAX_CHECKLIST_ITEMS + 1))
    with pytest.raises(ValidationError):
        NewTask(title="Clean", checklist=[""])
    due = datetime(2026, 12, 1, 10, tzinfo=UTC)
    assert NewTask(title="Clean", due_at=due).due_at == due


def test_new_capabilities_and_write_marks() -> None:
    assert Capability.PHOTOS_WRITE in WRITE_CAPABILITIES
    assert Capability.INBOX_NOTE in WRITE_CAPABILITIES
    assert Capability.PHOTOS_READ not in WRITE_CAPABILITIES
    assert Capability.PRICING_READ not in WRITE_CAPABILITIES
    assert Capability.LISTING_DETAILS_READ not in WRITE_CAPABILITIES


def test_fake_provider_still_conforms_with_new_domains() -> None:
    provider = FakeProvider([])
    assert isinstance(provider, PMSProvider)
    assert provider.content is None
    assert provider.pricing is None
