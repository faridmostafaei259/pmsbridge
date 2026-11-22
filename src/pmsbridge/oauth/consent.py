"""The connection choice on the consent page.

Until PMS connections exist, `choices_for` returns none, and the consent page says connections
can be chosen once they exist. An approval given while a person has no connections covers no
connections (`all_connections` false, empty list): it does not extend to connections added later.
The PMS feature installs its source from its own `register(app)` with `set_choice_source`, and
the page, the form and the grant storage need no other change.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from pmsbridge.oauth.grants import Selection

MAX_SELECTED: Final = 100


@dataclass(frozen=True)
class ConnectionChoice:
    """One connection the person may allow. `id` is stored in the grant; `label` is shown."""

    id: str
    label: str


ChoiceSource = Callable[[int], Awaitable[Sequence[ConnectionChoice]]]


async def _no_connections(_user_id: int) -> Sequence[ConnectionChoice]:
    return ()


_sources: list[ChoiceSource] = [_no_connections]


def set_choice_source(source: ChoiceSource) -> None:
    """Install the function that lists a person's connections (called by the PMS feature)."""
    _sources[0] = source


async def choices_for(user_id: int) -> list[ConnectionChoice]:
    return list(await _sources[0](user_id))


def read_selection(
    choices: Sequence[ConnectionChoice], pairs: Sequence[tuple[str, str]]
) -> Selection | None:
    """The person's choice from the submitted form, or None if it is not acceptable.

    With no connections to choose from, the grant covers none. Otherwise the form says
    `mode=all` or `mode=selected` with one `connection` field per ticked connection, each of which
    must be one of the person's own choices.
    """
    if not choices:
        return Selection(all_connections=False, connection_ids=[])
    fields = dict(reversed(pairs))
    if fields.get("mode") == "all":
        return Selection(all_connections=True, connection_ids=[])
    if fields.get("mode") != "selected":
        return None
    offered = {choice.id for choice in choices}
    picked = list(dict.fromkeys(value for name, value in pairs if name == "connection"))
    if not picked or len(picked) > MAX_SELECTED or any(value not in offered for value in picked):
        return None
    return Selection(all_connections=False, connection_ids=picked)
