"""Resolve indirect asset URLs once and pin their network destination."""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class UnsafeAssetURLError(ValueError):
    pass


class FakeIPAssetURLError(UnsafeAssetURLError):
    def __init__(self, hostname: str) -> None:
        super().__init__("generated asset URL points to a non-public address")
        self.hostname = hostname


_FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(),
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80))


@dataclass(frozen=True)
class AssetDestination:
    urls: tuple[str, ...]
    host_header: str
    server_hostname: str


def resolve_asset_destination(
    url: str, provider_base_url: str, *, resolved_addresses: Sequence[str] | None = None,
) -> AssetDestination:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        if (parsed.scheme.lower() not in {"http", "https"} or not hostname
                or parsed.username is not None or parsed.password is not None
                or "%" in hostname or "\\" in url or not 1 <= port <= 65535):
            raise ValueError("invalid URL")
        # The user explicitly trusts the configured provider origin. Its own
        # assets may be local; a redirect to any other origin gets no exception.
        allow_local = _origin(url) == _origin(provider_base_url)
        addresses = list(dict.fromkeys(resolved_addresses if resolved_addresses is not None else (
            str(entry[4][0]) for entry in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        )))
        if not addresses:
            raise ValueError("no destination")
        try:
            ipaddress.ip_address(hostname)
            domain_name = False
        except ValueError:
            domain_name = True
        if not allow_local and domain_name and all(
            ipaddress.ip_address(address).version == 4
            and ipaddress.ip_address(address) in _FAKE_IP_RANGE for address in addresses
        ):
            raise FakeIPAssetURLError(hostname)
        for address in addresses:
            ip = ipaddress.ip_address(address)
            mapped = getattr(ip, "ipv4_mapped", None)
            if not allow_local and (
                not ip.is_global or ip.is_multicast or ip.is_reserved or getattr(ip, "is_site_local", False)
                or (mapped is not None and not mapped.is_global)
                or getattr(ip, "sixtofour", None) is not None
                or getattr(ip, "teredo", None) is not None
            ):
                raise UnsafeAssetURLError("generated asset URL points to a non-public address")
        pinned_urls = []
        for address in addresses:
            authority = f"[{address}]" if ":" in address else address
            pinned_urls.append(urlunsplit((parsed.scheme, f"{authority}:{port}", parsed.path or "/", parsed.query, "")))
        return AssetDestination(tuple(pinned_urls), parsed.netloc, hostname)
    except UnsafeAssetURLError:
        raise
    except (ValueError, OSError) as exc:
        raise UnsafeAssetURLError("generated asset URL could not be safely resolved") from exc
