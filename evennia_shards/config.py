"""Read shards-related configuration from Django settings.

Always use these accessors rather than reading `settings.SHARDS_ROLE` /
`settings.SHARD_ID` directly. Direct reads raise `AttributeError` when
the consumer has not declared the setting — which is the common monolith
case. The accessors apply the documented defaults and are the single
source of truth for those fallback values.

Both library code and consumer game code that needs to introspect the
current deployment role or shard id should call these.
"""

DEFAULT_ROLE = "monolith"
DEFAULT_MESSAGE_TIMEOUT = 10


def get_role() -> str:
    """Return the current `SHARDS_ROLE`, defaulting to `"monolith"`.

    Prefer this over `settings.SHARDS_ROLE`; see module docstring.
    """
    from django.conf import settings

    return getattr(settings, "SHARDS_ROLE", DEFAULT_ROLE)


def get_shard_id() -> str | None:
    """Return the current `SHARD_ID`, defaulting to `None`.

    Prefer this over `settings.SHARD_ID`; see module docstring.
    """
    from django.conf import settings

    return getattr(settings, "SHARD_ID", None)


def get_router_url() -> str:
    """Return the webclient base URL for the router.

    Reads from the consumer's ``ROUTER_URL`` setting.

    Raises ``ValueError`` if ``ROUTER_URL`` is not configured.
    """
    from django.conf import settings

    url = getattr(settings, "ROUTER_URL", None)
    if url is None:
        raise ValueError(
            "ROUTER_URL is not configured. Define the router's base URL "
            "in your Django settings."
        )
    return url


def get_shard_url(shard_id: str) -> str:
    """Return the webclient base URL for `shard_id`.

    Reads from the consumer's ``SHARD_URLS`` setting — a dict mapping
    shard IDs to base URLs (e.g. ``{"shard0": "http://host:4001"}``).

    Raises ``KeyError`` if the shard ID is not found, ``ValueError``
    if ``SHARD_URLS`` is not configured.
    """
    from django.conf import settings

    urls = getattr(settings, "SHARD_URLS", None)
    if urls is None:
        raise ValueError(
            "SHARD_URLS is not configured. Define a dict mapping shard IDs "
            "to base URLs in your Django settings."
        )
    return urls[shard_id]


def get_message_timeout(kind: str) -> int:
    """Return the message-bus timeout (seconds) for `kind`.

    Resolution: per-kind override map (`SHARDS_MESSAGE_TIMEOUTS`) first,
    then the global default (`SHARDS_MESSAGE_TIMEOUT_DEFAULT`, library
    default 10s).
    """
    from django.conf import settings

    overrides = getattr(settings, "SHARDS_MESSAGE_TIMEOUTS", {})
    if kind in overrides:
        return overrides[kind]
    return getattr(settings, "SHARDS_MESSAGE_TIMEOUT_DEFAULT", DEFAULT_MESSAGE_TIMEOUT)
