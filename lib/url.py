"""URL normalization shared by customer clients."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit


def normalize_client_url(
    value: str,
    domain_validator: Callable[[str], str],
) -> tuple[str, bool]:
    """Normalize client URL input and report bare-FQDN conversion.

    Args:
        value: Positional URL or bare FQDN supplied by the user.
        domain_validator: Existing CLI validator used to recognize valid FQDNs.

    Returns:
        Tuple containing normalized URL and whether a bare FQDN was converted.
    """
    parsed = urlsplit(value)
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return value, False
        if parsed.path:
            return value, False
        return (
            urlunsplit(
                (parsed.scheme, parsed.netloc, "/", parsed.query, parsed.fragment)
            ),
            False,
        )

    if parsed.query or parsed.fragment or "/" in value:
        return value, False
    try:
        domain_validator(value)
    except argparse.ArgumentTypeError:
        return value, False
    return f"https://{value}/", True
