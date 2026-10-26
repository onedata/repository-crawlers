"""
Confline configuration classes for the registrar CLI.

Three logical layers:

- `CommonConfig` — connection/auth/output/logging shared by every command.
- Per-command configs — `RegisterConfig`, `ListSpacesConfig`,
  `ListStoragesConfig` — extend `CommonConfig` with command-specific fields.
- Mutex groups — `SpaceSelection`, `StorageSelection` — encode the
  name-or-ID alternatives so confline auto-renders "at most one of …" errors.
"""

__author__ = "Bartosz Walkowicz"
__copyright__ = "Copyright (C) 2026 Onedata (onedata.org)"
__license__ = "This software is released under the MIT license cited in LICENSE.txt"

from pathlib import Path
from typing import Annotated, Literal

from confline import (
    ConfigBase,
    MutuallyExclusiveGroup,
    model_validator,
    opt,
)
from confline.sources import CliPositional

# ─────────────────────────────────────────────────────────────────────────────
# Shared sub-configs
# ─────────────────────────────────────────────────────────────────────────────


class OnedataConnection(ConfigBase):
    """Connection settings for Onedata services."""

    onezone_domain: str = opt(
        "demo.onedata.org",
        description="Onezone domain used for space and public-data-record operations.",
    )
    oneprovider_domain: str = opt(
        "provider.demo.onedata.org",
        description=(
            "Oneprovider that registers crawled files via a remote, read-only HTTP storage."
        ),
    )
    oneprovider_panel_port: int = opt(
        443,
        description="Onepanel HTTPS API port on the selected Oneprovider.",
    )
    verify_ssl: bool = opt(
        False,
        description="Verify TLS certificates when talking to Onedata services.",
    )
    timeout: int = opt(
        30,
        description=(
            "HTTP request timeout in seconds for Onezone, Oneprovider, and Onepanel API calls."
        ),
    )


class Tokens(ConfigBase):
    """Authentication tokens for Onedata APIs."""

    admin_token: str = opt(
        "",
        description="Onepanel admin token (storage/support operations).",
        secret=True,
    )
    space_owner_token: str = opt(
        "",
        description="Onezone/Oneprovider user token (spaces, files, shares, handles).",
        secret=True,
    )


class Output(ConfigBase):
    """Output and report destinations for `register`."""

    dir: Path = opt(
        Path("./data"),
        description="Directory for run artifacts (logs, config dump, summary).",
    )


class Logging(ConfigBase):
    """Logging verbosity. `quiet` and `verbose` are convenience overrides of `level`."""

    level: Literal["debug", "info", "warning", "error", "silent"] = opt(
        "info",
        description="Explicit logging level.",
    )
    quiet: bool = opt(
        False,
        description="Show warnings, errors, and the final summary only.",
    )
    verbose: bool = opt(
        False,
        description="Show debug-level output.",
    )


class CommonConfig(ConfigBase):
    """Settings shared by every command."""

    onedata: OnedataConnection = opt(default_factory=OnedataConnection)
    tokens: Tokens = opt(default_factory=Tokens)
    logging: Logging = opt(default_factory=Logging)


# ─────────────────────────────────────────────────────────────────────────────
# Mutex groups for register
# ─────────────────────────────────────────────────────────────────────────────


class SpaceSelection(MutuallyExclusiveGroup, required=False):
    """Pick the destination space — by name (use-or-create) or by ID (use-existing)."""

    name: str = opt(
        "",
        description=(
            "Use or create a space with this exact name. When both `name` and "
            "`id` are empty, infer the name from the first file URL in the input."
        ),
    )
    id: str = opt(
        "",
        description="Use a specific existing space by its ID.",
    )


class StorageSelection(MutuallyExclusiveGroup, required=False):
    """Pick the storage backing the space — by name or by ID."""

    name: str = opt(
        "",
        description=(
            "Use or create an HTTP readonly storage with this exact name. "
            "When empty, reuse the sole compatible storage or create one."
        ),
    )
    id: str = opt(
        "",
        description="Use a specific existing storage by its ID.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Storage parameters (create-time defaults + per-run validation policy)
# ─────────────────────────────────────────────────────────────────────────────


class StorageOptions(ConfigBase):
    """HTTP storage parameters used at create time and validated against an existing storage.

    `default_size` is consumed by `support_space`; the `emulate_range_read`
    pair maps to the Onepanel `add_storage` payload and is also checked
    against the chosen pre-existing storage during planning.
    """

    default_size: int = opt(
        1099511627776,  # 1 TiB
        description=(
            "Default support size in bytes used when this run adds storage "
            "support for the resolved space."
        ),
    )
    emulate_range_read: bool = opt(
        False,
        description=(
            "Emulate HTTP Range requests for servers that lack native Range "
            "support. Significant performance hit — every read downloads the "
            "whole file."
        ),
    )
    max_emulated_range_read_file_size: int | None = opt(
        None,
        description=(
            "Maximum file size in bytes accessible from servers without Range "
            "support. Active only when `emulate_range_read` is true. Leave "
            "unset to defer to the storage's own value (or the Onepanel "
            "default at create time)."
        ),
    )

    @model_validator
    def _max_size_requires_emulate(self) -> None:
        if self.max_emulated_range_read_file_size is not None and not self.emulate_range_read:
            raise ValueError(
                "storage_options.max_emulated_range_read_file_size requires "
                "storage_options.emulate_range_read=true.",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Public-data-records / sharing
# ─────────────────────────────────────────────────────────────────────────────


class PublicDataRecords(ConfigBase):
    """Sharing and public-data-record policy applied to every dataset."""

    enabled: bool = opt(
        True,
        description=(
            "Create a public data record per dataset share (metadata + PID). "
            "Set false to create only basic Onedata shares."
        ),
    )
    handle_service_id: str = opt(
        "",
        description=(
            "Onedata handle service ID for public data records. Required when `enabled` is true."
        ),
    )
    record_identifier_type: Literal["onedata-url", "pid"] = opt(
        "onedata-url",
        description=(
            "Identifier kind to mint: `onedata-url` reuses the share URL; "
            "`pid` requests a handle from the configured handle service."
        ),
    )
    identifier_policy: Literal[
        "always-generate-new",
        "always-reuse-existing",
        "generate-new-if-missing",
    ] = opt(
        "generate-new-if-missing",
        description=(
            "How to treat per-dataset `pid` values from the input: "
            "`always-generate-new` ignores them; "
            "`always-reuse-existing` requires every dataset to provide one; "
            "`generate-new-if-missing` reuses when present, mints otherwise."
        ),
    )

    @model_validator
    def _handle_service_requires_id(self) -> None:
        if self.enabled and not self.handle_service_id:
            raise ValueError(
                "enabled=true requires public_data_records.handle_service_id.",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Per-command configs
# ─────────────────────────────────────────────────────────────────────────────


class RegisterConfig(CommonConfig):
    """Configuration for `registrar register`."""

    output: Output = opt(default_factory=Output)
    datasets_file: Annotated[Path, CliPositional] = opt(
        description="Path to the JSON or JSONL file with the datasets to register.",
    )
    space: SpaceSelection = opt(default_factory=SpaceSelection)
    storage: StorageSelection = opt(default_factory=StorageSelection)
    storage_options: StorageOptions = opt(default_factory=StorageOptions)
    dataset_root: str = opt(
        "",
        description=(
            "Relative path inside the space where dataset directories will be created. "
            "Empty means each dataset's `target_dir` is the top-level directory."
        ),
    )
    public_data_records: PublicDataRecords = opt(default_factory=PublicDataRecords)
    amend: bool = opt(
        False,
        description=(
            "Update changed dataset metadata handles and re-register files whose URLs have changed."
        ),
    )


class ListSpacesConfig(CommonConfig):
    """Configuration for `registrar list-spaces`."""


class ListStoragesConfig(CommonConfig):
    """Configuration for `registrar list-storages`."""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def effective_log_level(logging: Logging) -> str:
    """Resolve `quiet` / `verbose` convenience flags into a concrete level.

    `verbose` wins over `quiet` (matches the legacy CLI). When neither is
    set, the explicit `level` field is returned unchanged.
    """
    if logging.verbose:
        return "debug"
    if logging.quiet:
        return "warning"
    return logging.level
