# SPDX-License-Identifier: BSD-3-Clause
"""Shard-aware character creation.

Vanilla Evennia's ``Account.create_character`` runs on the router (it's
an account-side flow; ``CmdCharCreate``, ``AUTO_CREATE_CHARACTER_WITH_ACCOUNT``,
and the guest path all funnel through it). Without intervention, the new
row is auto-stamped with ``shard_id="router"`` by the ``pre_save``
chokepoint — making the character un-IC-able, since ``"router"`` is not
a member of ``SHARD_URLS``.

``make_shard_aware_create_character`` returns a shallow wrapper around
vanilla ``create_character`` that, on success, looks up the new
character's start-location row, reads its ``shard_id``, and stamps the
character to match. The two rows must agree: a character whose location
sits on a different shard would trip the ``from_db`` chokepoint the
first time anything dereferences ``db_location``.

The wrapper deliberately does not interfere with vanilla's body or
kwargs — any consumer or upstream churn in chargen is invisible here.
"""

from evennia.utils import logger


def make_shard_aware_create_character(original_create_character):
    """Return a router-side ``create_character`` that stamps ``shard_id``.

    The returned wrapper:

    1. Calls ``original_create_character(self, *args, **kwargs)``.
    2. On a falsy character return (vanilla refused), passes the tuple
       through unchanged.
    3. Otherwise reads the start-location row's ``shard_id`` via
       ``.values_list`` (no ``from_db`` instantiation), and if usable
       — i.e. not ``None``, not the global ``"*"`` sentinel, and not the
       router's own shard id — overwrites the character's auto-stamped
       ``shard_id`` and saves with ``update_fields=["shard_id"]``.
    4. On unusable lookups, logs a warning and returns the character
       unchanged. Chargen has succeeded; the misconfiguration surfaces
       in logs and at the next IC attempt.

    The router is exempt from the ``pre_save`` chokepoint's foreign-shard
    refusal, so the second save lands without a bypass.
    """
    from evennia.objects.models import ObjectDB

    from .config import ROUTER_SHARD_ID

    def shard_aware_create_character(self, *args, **kwargs):
        character, errs = original_create_character(self, *args, **kwargs)
        if not character:
            return character, errs

        location_id = character.db_location_id
        if location_id is None:
            logger.log_warn(
                f"shard_aware_create_character: new character "
                f"pk={character.pk!r} has no db_location; leaving shard_id "
                f"as auto-stamped (chargen succeeded but the character "
                f"will not be IC-able until shard_id is corrected)"
            )
            return character, errs

        rows = list(
            ObjectDB.objects.filter(pk=location_id)
            .values_list("shard_id", flat=True)[:1]
        )
        location_shard = rows[0] if rows else None

        if (
            not location_shard
            or location_shard == "*"
            or location_shard == ROUTER_SHARD_ID
        ):
            logger.log_warn(
                f"shard_aware_create_character: start location "
                f"pk={location_id!r} has unusable shard_id="
                f"{location_shard!r} (expected a real shard id); "
                f"leaving character pk={character.pk!r} as auto-stamped"
            )
            return character, errs

        character.shard_id = location_shard
        character.save(update_fields=["shard_id"])
        return character, errs

    return shard_aware_create_character
