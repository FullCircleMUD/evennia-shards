# SPDX-License-Identifier: BSD-3-Clause
"""Shard-aware global search.

Substitute for ``caller.search(name, global_search=True)`` in code that
must run on a sharded process. Vanilla Evennia's global search returns
candidate rows from every shard and instantiates them; on a non-monolith
process, instantiation of a foreign-shard row trips the ``from_db``
chokepoint and raises ``ShardIsolationError``.

The helper here does the lookup in two steps:

1. SQL-level ``values_list`` for ``(pk, shard_id, db_key)`` of every row
   whose ``db_key`` matches the input name (or whose dbref matches, when
   the name is ``#<int>``). This query is chokepoint-safe — no
   ``from_db``, no row instantiation.
2. Once the match's shard is known:
   - If the match is local (current shard or the global ``"*"`` sentinel),
     load the instance via the regular ORM so callers get full Evennia
     semantics for the local path.
   - If the match is on another shard, return the metadata only —
     callers route via the library's cross-shard primitives instead of
     loading.

The result is a :class:`ShardSearchResult` with explicit ``state``
(``"found"`` / ``"not_found"`` / ``"multiple"``) and the per-match data
the caller needs to dispatch.

Scope note: this initial cut handles dbref lookups (``#42``) and
case-insensitive exact ``db_key`` matches. Alias matching, partial-name
matching, and "here"/"me" specials that vanilla ``caller.search``
supports are not implemented yet; if a consumer needs them the helper
can grow without changing the call shape.
"""

from dataclasses import dataclass, field
from typing import Optional

from .config import get_shard_id


@dataclass
class ShardSearchResult:
    """Outcome of :func:`shard_aware_global_search`.

    Three mutually-exclusive states drive caller dispatch:

    - ``"found"``: exactly one match. ``pk``, ``shard_id``, ``db_key``
      are populated. ``obj`` is the loaded instance when the match is
      local; ``None`` when the match is on another shard.
    - ``"not_found"``: no match. All instance/metadata fields ``None``.
    - ``"multiple"``: more than one match. ``candidates`` holds a list
      of ``(pk, shard_id, db_key)`` triples for the caller to render
      disambiguation against.
    """

    state: str  # "found" | "not_found" | "multiple"
    obj: Optional[object] = None
    pk: Optional[int] = None
    shard_id: Optional[str] = None
    db_key: Optional[str] = None
    candidates: list = field(default_factory=list)

    @property
    def is_local(self) -> bool:
        """True iff the match is loadable on this process."""
        if self.state != "found":
            return False
        return self.shard_id == get_shard_id() or self.shard_id == "*"

    @property
    def is_cross_shard(self) -> bool:
        """True iff the match exists but lives on a different shard."""
        return self.state == "found" and not self.is_local


def shard_aware_global_search(
    caller,
    name: str,
    tag: Optional[str] = None,
    tag_category: Optional[str] = None,
) -> ShardSearchResult:
    """Look up an object by name across all shards, chokepoint-safely.

    Args:
        caller: the ``ObjectDB`` instance triggering the lookup. Currently
            unused — included for symmetry with ``caller.search`` so
            future locality / permission-scoped behaviour can be added
            without changing the signature.
        name: dbref string (``"#42"``) or db_key (case-insensitive
            exact match).
        tag: optional tag key to narrow the search to objects carrying
            this tag. When set, only rows with a matching tag are
            considered. The common consumer pattern is to scope the
            lookup to a zone (e.g. ``tag="millholm", tag_category="zone"``)
            so a single key can be reused across zones without ambiguity.
        tag_category: optional tag category. Only consulted when ``tag``
            is set. When omitted, any category for the given tag key
            matches.

    Returns:
        :class:`ShardSearchResult` — see class docstring for the
        state-driven contract.
    """
    from evennia.objects.models import ObjectDB

    name = name.strip()
    if not name:
        return ShardSearchResult(state="not_found")

    # dbref lookup
    pk_to_search: Optional[int] = None
    if name.startswith("#"):
        try:
            pk_to_search = int(name[1:])
        except ValueError:
            pk_to_search = None

    if pk_to_search is not None:
        qs = ObjectDB.objects.filter(pk=pk_to_search)
    else:
        qs = ObjectDB.objects.filter(db_key__iexact=name)

    # Optional tag filter. Composes onto the same queryset — Django
    # joins ObjectDB to its m2m Tag table and ANDs the predicates into
    # one SQL WHERE clause. Cross-shard scoping (the shard_id column on
    # ObjectDB) and tag filtering (the join through db_tags) are
    # orthogonal — each narrows the result set independently, and any
    # row that makes it past the filter still carries shard_id for the
    # routing logic below.
    if tag is not None:
        qs = qs.filter(db_tags__db_key=tag)
        if tag_category is not None:
            qs = qs.filter(db_tags__db_category=tag_category)

    # values_list keeps the query SQL-only — no per-row from_db.
    rows = list(qs.values_list("pk", "shard_id", "db_key"))

    if not rows:
        return ShardSearchResult(state="not_found")
    if len(rows) > 1:
        return ShardSearchResult(state="multiple", candidates=rows)

    matched_pk, matched_shard, matched_key = rows[0]
    is_local = matched_shard == get_shard_id() or matched_shard == "*"

    if is_local:
        # Load the instance via the regular ORM — safe because the row
        # is on this shard (or the global sentinel) and from_db will
        # accept it.
        try:
            obj = ObjectDB.objects.get(pk=matched_pk)
        except ObjectDB.DoesNotExist:
            # Race: row vanished between values_list and get. Treat as
            # not-found.
            return ShardSearchResult(state="not_found")
        return ShardSearchResult(
            state="found",
            obj=obj,
            pk=matched_pk,
            shard_id=matched_shard,
            db_key=matched_key,
        )

    # Cross-shard match — return metadata only; caller routes via
    # cross_shard_move or equivalent primitive.
    return ShardSearchResult(
        state="found",
        obj=None,
        pk=matched_pk,
        shard_id=matched_shard,
        db_key=matched_key,
    )
