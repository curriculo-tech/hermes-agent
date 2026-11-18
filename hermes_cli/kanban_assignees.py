"""The assignee registry: every name a Kanban card can carry and still dispatch.

Valid assignees are the union of dispatcher profiles
(``<hermes-root>/profiles/<name>/config.yaml``, plus the implicit ``default``)
and registered personas (``<hermes-root>/personas/<name>/SOUL.md`` — the
fleet-wake lanes). Intake surfaces (CLI ``create``/``assign``/``reassign`` and
the ``kanban_create`` tool) validate against this registry and reject empty or
unknown assignees: the dispatcher only claims cards carrying its own profile,
so such a card would sit in ``ready`` forever (``skipped_nonspawnable``).
Rejection — never auto-routing — surfaces the caller bug at the source.
"""
from __future__ import annotations

from typing import Optional


def list_persona_names() -> list[str]:
    """Registered personas — ``<hermes-root>/personas/<name>/SOUL.md``, the
    fleet-wake lanes. Reads paths directly (same rule as
    ``list_profiles_on_disk``) and never raises: an unreadable registry just
    lists nothing."""
    try:
        from hermes_constants import get_default_hermes_root
        personas_dir = get_default_hermes_root() / "personas"
    except Exception:  # health: allow BLE001 -- registry probe must never crash intake; same never-raise contract as list_profiles_on_disk
        return []

    names: set[str] = set()
    if personas_dir.is_dir():
        try:
            names.update(
                e.name for e in personas_dir.iterdir()
                if e.is_dir() and (e / "SOUL.md").is_file()
            )
        except OSError:
            pass
    return sorted(names)


def valid_assignees() -> list[str]:
    """Sorted union of dispatcher profiles (``list_profiles_on_disk``) and
    registered personas (``list_persona_names``) — every name a card can
    carry and still reach a worker."""
    from hermes_cli.kanban_db import list_profiles_on_disk
    return sorted(set(list_profiles_on_disk()) | set(list_persona_names()))


def validate_assignee(assignee: Optional[str]) -> str:
    """Return the stripped assignee when a dispatcher profile or registered
    persona answers to it; raise ``ValueError`` otherwise."""
    name = (assignee or "").strip()
    valid = valid_assignees()
    if not name:
        raise ValueError(
            "assignee is required — name a dispatcher profile or a registered persona; "
            f"valid: {', '.join(valid)}"
        )
    if name not in valid:
        raise ValueError(
            f"assignee '{name}' is not a dispatcher profile or registered persona — "
            f"card would never dispatch; valid: {', '.join(valid)} "
            "(personas are fleet-wake lanes; registry: ~/.hermes/personas/<name>/SOUL.md)"
        )
    return name
