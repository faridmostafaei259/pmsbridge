from pmsbridge.accounts.reauth import MAX_ENTRIES, REAUTH_SECONDS, ReauthStore


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_confirmation_lasts_ten_minutes_exactly() -> None:
    clock = Clock()
    store = ReauthStore(clock)
    assert not store.is_recent("a")
    store.mark("a")
    clock.now += REAUTH_SECONDS - 1
    assert store.is_recent("a")
    clock.now += 1
    assert not store.is_recent("a")


def test_expired_entries_are_evicted_when_the_next_one_is_added() -> None:
    clock = Clock()
    store = ReauthStore(clock)
    store.mark("old")
    store.mark("older-but-fresh-later")
    clock.now += REAUTH_SECONDS + 1
    store.mark("new")
    assert len(store) == 1
    assert store.is_recent("new")


def test_the_store_never_holds_more_than_the_cap_and_drops_the_oldest() -> None:
    clock = Clock()
    store = ReauthStore(clock)
    for number in range(MAX_ENTRIES + 50):
        clock.now += 0.001
        store.mark(f"s{number}")
        assert len(store) <= MAX_ENTRIES
    assert len(store) == MAX_ENTRIES
    assert not store.is_recent("s0")
    assert store.is_recent(f"s{MAX_ENTRIES + 49}")


def test_forgetting_removes_one_session_and_is_safe_for_unknown_ones() -> None:
    store = ReauthStore(Clock())
    store.mark("a")
    store.mark("b")
    store.forget("a")
    store.forget("never-seen")
    assert not store.is_recent("a")
    assert store.is_recent("b")
