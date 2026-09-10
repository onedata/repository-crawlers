"""
VIP API thin façade over `HttpClient`.

Girder REST API client for VIP (Virtual Imaging Platform). Owns no
session state — pass an open `HttpClient` (with `base_url` set to the
Girder root) and reuse it across calls.
"""

__author__ = "Bartosz Walkowicz"
__copyright__ = "Copyright (C) 2026 Onedata (onedata.org)"
__license__ = "This software is released under the MIT license cited in LICENSE.txt"

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import assert_never

from crawlers.core import Err, HttpClient, HttpFailure, JsonObject, Ok, Result
from crawlers.ui import console


@dataclass
class VipFile:
    """A downloadable file within a VIP dataset folder."""

    path: str
    """Relative path from the dataset root (e.g. 'subdir/file.txt')."""
    url: str
    """Direct download URL via Girder item download endpoint."""


class VipClient:
    """
    Stateless façade over `HttpClient` for the VIP Girder REST API.

    Resolves a named collection to its ID, paginates top-level dataset
    folders, and recursively collects files for a given folder.
    """

    _COLLECTION_PAGE_SIZE = 100

    def __init__(self, http: HttpClient):
        self._http = http

    # --- Collections ---

    async def list_collections(self) -> Result[list[JsonObject], HttpFailure]:
        """
        Fetch all available collections, paginating until exhausted.

        Returns:
            Ok with Girder collection objects, or Err on HTTP/network error.
        """
        all_collections: list[JsonObject] = []
        offset = 0

        while True:
            url = (
                f"/collection?limit={self._COLLECTION_PAGE_SIZE}&offset={offset}"
                f"&sort=name&sortdir=1"
            )
            result = await self._http.get_json(url)
            if isinstance(result, Err):
                return result

            page = _as_girder_object_list(result.value)
            if not page:
                break

            all_collections.extend(page)
            if len(page) < self._COLLECTION_PAGE_SIZE:
                break
            offset += len(page)
            console.debug(
                f"Fetched {len(all_collections)} collections so far, fetching next page..."
            )

        console.info(f"Found {len(all_collections)} collection(s) total")
        return Ok(all_collections)

    # --- Iteration ---

    async def iterate_datasets(
        self,
        collection: str,
        *,
        page_size: int = 100,
        max_records: int | None = None,
    ) -> AsyncIterator[JsonObject]:
        """
        Iterate over top-level dataset folders in the named collection.

        Yields raw Girder folder dicts. File resolution is done separately
        via `resolve_dataset()` so it can run in parallel workers.
        """
        collection_id = await self._resolve_collection_id(collection)
        if not collection_id:
            console.error(f"Collection not found: {collection!r}")
            return

        total = await self._get_collection_folder_count(collection_id)
        console.info(f"Found {total} datasets in collection {collection!r}")

        yielded = 0
        offset = 0

        while offset < total:
            folders = await self._list_folders(
                parent_type="collection",
                parent_id=collection_id,
                limit=page_size,
                offset=offset,
            )
            if not folders:
                break

            for folder in folders:
                if max_records is not None and yielded >= max_records:
                    console.info(f"Reached max_records limit: {max_records}")
                    return

                yield folder
                yielded += 1

            offset += len(folders)

    # --- Dataset resolution ---

    async def resolve_dataset_files(
        self,
        folder: JsonObject,
    ) -> tuple[list[VipFile], dict[str, JsonObject]]:
        """
        Recursively collect all downloadable files under a Girder folder.

        Returns a flat list of `VipFile` with paths relative to the
        dataset root.
        """
        folder_id = folder.get("_id", "")
        folder_name = folder.get("name", folder_id)

        console.debug(f"Resolving files for dataset: {folder_name}")
        files, folders_meta = await self._collect_files(folder_id, path_prefix="")
        console.info(f"Resolved {folder_name}: {len(files)} file(s)")
        console.debug(f"Resolving external config files for dataset: {folder_name}")
        external_config_files = await self._collect_external_config_files(folders_meta)
        if external_config_files:
            console.info(f"Resolved {folder_name}: {len(external_config_files)} external file(s)")
            files.extend(external_config_files)
        return files, folders_meta

    # --- Internal helpers ---

    async def _resolve_collection_id(self, name: str) -> str | None:
        """Look up collection ID by name."""
        result = await self.list_collections()
        match result:
            case Ok(value=collections):
                for coll in collections:
                    if coll.get("name") == name:
                        return coll.get("_id")
                available = [c.get("name", "?") for c in collections]
                console.error(f"Collection {name!r} not found. Available: {', '.join(available)}")
                return None
            case Err(value=err):
                console.error(f"Failed to list collections: {err}")
                return None
            case other:
                assert_never(other)

    async def _get_collection_folder_count(self, collection_id: str) -> int:
        """Return the number of top-level folders in a collection."""
        match await self._http.get_json_object(f"/collection/{collection_id}/details"):
            case Ok(value=data):
                return int(data.get("nFolders", 0) or 0)
            case Err(value=err):
                console.warning(f"Failed to get collection details: {err}")
                return 0
            case other:
                assert_never(other)

    async def _get_folder_details(self, folder_id: str) -> tuple[int, int]:
        """Return (nFolders, nItems) for a folder."""
        match await self._http.get_json_object(f"/folder/{folder_id}/details"):
            case Ok(value=data):
                return int(data.get("nFolders", 0) or 0), int(data.get("nItems", 0) or 0)
            case Err(value=err):
                console.warning(f"Failed to get folder details for {folder_id}: {err}")
                return 0, 0
            case other:
                assert_never(other)

    async def _list_folders(
        self,
        parent_type: str,
        parent_id: str,
        limit: int,
        offset: int,
    ) -> list[JsonObject]:
        """Fetch one page of child folders."""
        url = (
            f"/folder?limit={limit}&offset={offset}&sort=name&sortdir=1"
            f"&parentType={parent_type}&parentId={parent_id}"
        )
        match await self._http.get_json(url):
            case Ok(value=data):
                return _as_girder_object_list(data)
            case Err(value=err):
                console.warning(f"Failed to list folders ({parent_type}/{parent_id}): {err}")
                return []
            case other:
                assert_never(other)

    async def _list_items(self, folder_id: str, limit: int, offset: int) -> list[JsonObject]:
        """Fetch one page of items (files) inside a folder."""
        url = f"/item?limit={limit}&offset={offset}&sort=name&sortdir=1&folderId={folder_id}"
        match await self._http.get_json(url):
            case Ok(value=data):
                return _as_girder_object_list(data)
            case Err(value=err):
                console.warning(f"Failed to list items for folder {folder_id}: {err}")
                return []
            case other:
                assert_never(other)

    async def _collect_files(
        self,
        folder_id: str,
        path_prefix: str,
        page_size: int = 100,
        folders_meta: dict[str, JsonObject] | None = None,
    ) -> tuple[list[VipFile], dict[str, JsonObject]]:
        """Recursively collect all downloadable files under a folder."""
        files: list[VipFile] = []
        folders_meta = folders_meta or {}
        n_folders, n_items = await self._get_folder_details(folder_id)

        if n_items > 0:
            offset = 0
            while True:
                items = await self._list_items(folder_id, page_size, offset)
                if not items:
                    break
                for item in items:
                    item_id = str(item.get("_id", "") or "")
                    item_name = str(item.get("name", item_id) or item_id)
                    file_path = f"{path_prefix}/{item_name}" if path_prefix else item_name
                    base = (self._http.base_url or "").rstrip("/")
                    item_url = f"{base}/item/{item_id}/download"
                    files.append(VipFile(path=file_path, url=item_url))
                    match await self._http.get_json_object(f"/item/{item_id}"):
                        case Ok(value=data):
                            folders_meta[file_path] = data
                        case Err(value=err):
                            console.warning(f"Failed to get collection details: {err}")
                offset += len(items)
                if offset >= n_items:
                    break

        if n_folders > 0:
            offset = 0
            while True:
                subfolders = await self._list_folders("folder", folder_id, page_size, offset)
                if not subfolders:
                    break
                for subfolder in subfolders:
                    sub_id = str(subfolder.get("_id", "") or "")
                    sub_name = str(subfolder.get("name", sub_id) or sub_id)
                    sub_prefix = f"{path_prefix}/{sub_name}" if path_prefix else sub_name
                    folders_meta[sub_prefix] = subfolder
                    files_layer, folders_meta_layer = await self._collect_files(
                        sub_id, sub_prefix, page_size, folders_meta
                    )
                    files.extend(files_layer)
                    folders_meta.update(folders_meta_layer)
                offset += len(subfolders)
                if offset >= n_folders:
                    break

        return files, folders_meta

    async def _collect_external_config_files(
        self, folders_meta: dict[str, JsonObject] | None = None
    ) -> list[VipFile]:
        """Collect external config directory files, specific to this dataset"""
        folders_meta = folders_meta or {}
        steam_press: list[str] = [
            key for key in folders_meta if re.search(r"(STEAM[^/.]*|PRESS[^/.]*)$", key)
        ]
        config_folder_id = None
        config_folder_path = None
        for key in steam_press:
            acq_meta = folders_meta[key].get("meta", {})
            if acq_meta.get("config") is not None:
                config: JsonObject = acq_meta.get("config")
                config_folder_id = config.get("folder_id")
                config_folder_path = config.get("path")
                break
        if config_folder_id is None or config_folder_path is None:
            console.warning("Failed to associate config files to this dataset")
            return []
        external_files, _ = await self._collect_files(
            config_folder_id, path_prefix=f"config/{config_folder_path.split('/')[-1]}"
        )
        return external_files


def _as_girder_object_list(value: object) -> list[JsonObject]:
    """Girder list endpoints return a JSON array of resource objects."""
    if not isinstance(value, list):
        return []
    return [obj for obj in value if isinstance(obj, dict)]
