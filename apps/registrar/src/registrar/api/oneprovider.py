"""
Oneprovider REST client — data-plane operations: file registration, lookups, shares.
"""

__author__ = "Bartosz Walkowicz"
__copyright__ = "Copyright (C) 2026 Onedata (onedata.org)"
__license__ = "This software is released under the MIT license cited in LICENSE.txt"

import logging
from typing import Final

import requests

from registrar.api.utils import (
    DEFAULT_TIMEOUT,
    disable_ssl_warnings,
    handle_error,
    require_token,
)
from registrar.config import CommonConfig

logger = logging.getLogger(__name__)

disable_ssl_warnings()

SERVICE_NAME: Final[str] = "Oneprovider"


class OneproviderClient:
    """Client for the Oneprovider REST API."""

    def __init__(
        self,
        domain: str,
        token: str,
        verify_ssl: bool = False,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.domain = domain
        self.token = token
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._base_url = f"https://{domain}/api/v3/oneprovider"

    @classmethod
    def from_config(cls, config: CommonConfig) -> "OneproviderClient":
        """Build an `OneproviderClient` from the resolved config.

        Requires `tokens.space_owner_token`; raises `MissingTokenError` otherwise.
        """
        return cls(
            domain=config.onedata.oneprovider_domain,
            token=require_token(
                config.tokens.space_owner_token,
                path="tokens.space_owner_token",
            ),
            verify_ssl=config.onedata.verify_ssl,
            timeout=config.onedata.timeout,
        )

    def _headers(self) -> dict:
        return {"X-Auth-Token": self.token}

    def _headers_json(self) -> dict:
        return {"X-Auth-Token": self.token, "Content-Type": "application/json"}

    # ─────────────────────────────────────────────────────────────────────────
    # File operations
    # ─────────────────────────────────────────────────────────────────────────

    def lookup_file_id(self, space_name: str, path: str) -> str | None:
        """Resolve a `space_name`/`path` pair to a file ID, or `None` on miss."""
        path = path.lstrip("/")
        url = f"{self._base_url}/lookup-file-id/{space_name}/{path}"

        response = requests.post(
            url=url,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        if response.ok and "fileId" in response.text:
            file_id: str | None = response.json().get("fileId")
            return file_id

        return None

    def get_file_storage_location(self, file_id: str) -> str | None:
        """Returns information about file location."""
        url = f"{self._base_url}/data/{file_id}/storage_locations"
        response = requests.get(
            url=url,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=self.timeout,
        )

        if response.ok:
            data: dict = response.json()
            location = next(
                (
                    storage.get("location")
                    for provider in data.get(
                        "locationsPerProvider",
                        {},
                    ).values()
                    for storage in provider.get(
                        "locationsPerStorageBackend",
                        {},
                    ).values()
                    if storage.get("location")
                ),
                None,
            )
            return location
        return None

    def get_file_attrs(self, file_id: str) -> dict | None:
        """Fetch file attributes (including `shares`), or `None` when missing."""
        url = f"{self._base_url}/data/{file_id}"
        response = requests.get(
            url=url,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        if response.ok:
            attrs: dict | None = response.json()
            return attrs

        return None

    def register_file(  # noqa: PLR0913 — REST shape, kwargs-only by design
        self,
        *,
        storage_id: str,
        space_id: str,
        file_url: str,
        dest_path: str,
        size: int | None = None,
        auto_detect: bool = True,
    ) -> str:
        """Register a remote file under `dest_path` and return its file ID.

        `file_url` is taken verbatim as `storageFileId` — the registrar only
        targets HTTP readonly storages, where the URL doubles as the path.
        Pass `auto_detect=False` together with `size` to skip the HEAD probe.
        """
        url = f"{self._base_url}/data/register"
        payload: dict[str, object] = {
            "storageId": storage_id,
            "spaceId": space_id,
            "storageFileId": file_url,
            "destinationPath": dest_path,
            "autoDetectAttributes": auto_detect,
        }
        if not auto_detect and size is not None:
            payload["size"] = size

        response = requests.post(
            url=url,
            headers=self._headers_json(),
            json=payload,
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        handle_error(response, service=SERVICE_NAME)

        file_id: str = response.json().get("fileId")
        logger.debug("Registered file: %s -> %s", dest_path, file_id)
        return file_id

    def remove_file(self, file_id: str) -> None:
        """Removes file specified by file ID"""
        url = f"{self._base_url}/data/{file_id}"
        response = requests.delete(
            url=url,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        handle_error(response, service=SERVICE_NAME)

    # ─────────────────────────────────────────────────────────────────────────
    # Share operations
    # ─────────────────────────────────────────────────────────────────────────

    def create_share(self, file_id: str, name: str, description: str = "") -> str:
        """Create a public share for a file or directory and return its ID."""
        url = f"{self._base_url}/shares"
        response = requests.post(
            url=url,
            headers=self._headers_json(),
            json={"fileId": file_id, "name": name, "description": description},
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        handle_error(response, service=SERVICE_NAME)

        share_id: str = response.json().get("shareId")
        logger.info("Created share '%s' with ID: %s", name, share_id)
        return share_id

    def get_share_details(self, share_id: str) -> dict | None:
        """Fetch share details (including `handleId`, `publicUrl`), or `None` on miss."""
        url = f"{self._base_url}/shares/{share_id}"
        response = requests.get(
            url=url,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        if response.ok:
            details: dict | None = response.json()
            return details
        return None
