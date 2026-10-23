"""
Registration loop — run the resolved target against a list of datasets.

Public surface:

- `run_registration` — orchestrator called by `RegistrarApp.register` after
  the plan has been confirmed and applied.
- `RecordRequirementError` — typed per-dataset failure for the
  identifier-policy / public-identifier-type combinations a dataset cannot
  satisfy. The runner converts it into a failed outcome and continues.
"""

__author__ = "Bartosz Walkowicz"
__copyright__ = "Copyright (C) 2026 Onedata (onedata.org)"
__license__ = "This software is released under the MIT license cited in LICENSE.txt"

from collections.abc import Sequence

import requests

from onedata_dataset import OnedataDataset
from registrar.api.oneprovider import OneproviderClient
from registrar.api.onezone import OnezoneClient
from registrar.config import PublicDataRecords, RegisterConfig
from registrar.register.types import DatasetOutcome, FailedDataset, ResolvedTarget, Summary
from registrar.ui.progress_sink import NullProgressSink, ProgressSink

# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────


class RecordRequirementError(RuntimeError):
    """A dataset cannot satisfy the configured identifier policy.

    Raised per-dataset; the runner converts it into a failed
    `DatasetOutcome` and continues with the next dataset.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def run_registration(  # noqa: PLR0913
    target: ResolvedTarget,
    datasets: Sequence[OnedataDataset],
    config: RegisterConfig,
    *,
    oneprovider: OneproviderClient,
    onezone: OnezoneClient,
    progress_sink: ProgressSink | None = None,
) -> Summary:
    """Walk `datasets`, register each, and return the aggregated `Summary`."""
    sink = progress_sink or NullProgressSink()
    total = len(datasets)
    successful = 0
    files_registered = 0
    files_skipped = 0
    shares_count = 0
    records_count = 0
    failures: list[FailedDataset] = []

    for idx, dataset in enumerate(datasets, 1):
        file_count = len(dataset.files) if hasattr(dataset, "files") else 0
        sink.start_dataset(idx, total, dataset.name or "(unnamed)", file_count)

        outcome = _process_one(target, dataset, config, oneprovider, onezone, sink)
        sink.finish_dataset(outcome)

        if outcome.success:
            successful += 1
            files_registered += outcome.files_registered
            files_skipped += outcome.files_skipped
            if outcome.share_id:
                shares_count += 1
            if outcome.record_identifier:
                records_count += 1
        else:
            failures.append(FailedDataset(name=outcome.name, error=outcome.error or ""))

    return Summary(
        total=total,
        successful=successful,
        files_registered=files_registered,
        files_skipped=files_skipped,
        shares_count=shares_count,
        records_count=records_count,
        failures=tuple(failures),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-dataset orchestration
# ─────────────────────────────────────────────────────────────────────────────


def _process_one(  # noqa: PLR0913
    target: ResolvedTarget,
    dataset: OnedataDataset,
    config: RegisterConfig,
    oneprovider: OneproviderClient,
    onezone: OnezoneClient,
    sink: ProgressSink,
) -> DatasetOutcome:
    dataset_dir = _join_paths(target.dataset_root, dataset.target_dir)

    try:
        sink.advance_phase("files")
        registered, skipped = _register_files(
            target,
            dataset,
            dataset_dir,
            oneprovider,
            sink,
            config,
        )

        sink.advance_phase("share")
        share_id = _ensure_share(target, dataset, dataset_dir, oneprovider)

        record_identifier: str | None = None
        if config.public_data_records.enabled:
            sink.advance_phase("record")
            record_identifier = _ensure_public_record(
                config=config.public_data_records,
                dataset=dataset,
                share_id=share_id,
                onezone=onezone,
                oneprovider=oneprovider,
                amend=config.amend,
            )
    except RecordRequirementError as exc:
        return DatasetOutcome(name=dataset.name, success=False, error=str(exc))
    except requests.RequestException as exc:
        return DatasetOutcome(name=dataset.name, success=False, error=str(exc))
    except RuntimeError as exc:
        return DatasetOutcome(name=dataset.name, success=False, error=str(exc))

    return DatasetOutcome(
        name=dataset.name,
        success=True,
        files_registered=registered,
        files_skipped=skipped,
        share_id=share_id,
        record_identifier=record_identifier,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-step helpers — files, share, public-data record
# ─────────────────────────────────────────────────────────────────────────────


def _register_files(  # noqa: PLR0913
    target: ResolvedTarget,
    dataset: OnedataDataset,
    dataset_dir: str,
    oneprovider: OneproviderClient,
    sink: ProgressSink,
    config: RegisterConfig,
) -> tuple[int, int]:
    registered = 0
    skipped = 0
    for file_info in dataset.files:
        file_path = file_info.path.strip("/")
        dest_path = f"{dataset_dir}/{file_path}" if dataset_dir else file_path

        existing = oneprovider.lookup_file_id(space_name=target.space_name, path=dest_path)
        if existing:
            if config.amend:
                location = oneprovider.get_file_storage_location(existing)
                if location != file_info.url:
                    oneprovider.remove_file(existing)
                    oneprovider.register_file(
                        storage_id=target.storage_id,
                        space_id=target.space_id,
                        file_url=file_info.url,
                        dest_path=dest_path,
                    )
                    registered += 1
                    sink.tick_file()
                    continue
            skipped += 1
            sink.tick_file()
            continue

        oneprovider.register_file(
            storage_id=target.storage_id,
            space_id=target.space_id,
            file_url=file_info.url,
            dest_path=dest_path,
        )
        registered += 1
        sink.tick_file()
    return registered, skipped


def _ensure_share(
    target: ResolvedTarget,
    dataset: OnedataDataset,
    dataset_dir: str,
    oneprovider: OneproviderClient,
) -> str:
    file_id = oneprovider.lookup_file_id(space_name=target.space_name, path=dataset_dir)
    if not file_id:
        raise RuntimeError(
            f"could not look up dataset directory {dataset_dir!r} after file registration",
        )

    return _find_or_create_share(
        oneprovider,
        file_id=file_id,
        name=dataset.name,
        description=dataset.name,
    )


def _find_or_create_share(
    oneprovider: OneproviderClient,
    *,
    file_id: str,
    name: str,
    description: str,
) -> str:
    file_attrs = oneprovider.get_file_attrs(file_id) or {}
    for share_id in file_attrs.get("shares") or ():
        details = oneprovider.get_share_details(share_id) or {}
        if details.get("name") == name and details.get("description") == description:
            matched_share_id: str = share_id
            return matched_share_id

    return oneprovider.create_share(file_id=file_id, name=name, description=description)


def _ensure_public_record(  # noqa: PLR0913
    *,
    config: PublicDataRecords,
    dataset: OnedataDataset,
    share_id: str,
    onezone: OnezoneClient,
    oneprovider: OneproviderClient,
    amend: str,
) -> str:
    """Return the public-data-record identifier for `dataset`.

    Both `onedata-url` and `pid` paths go through `register_handle`.
    `public_identifier_type` controls `requestPublicHandle`;
    `identifier_policy` controls `publicHandleToReuse`.
    """
    if not dataset.metadata_xml:
        raise RecordRequirementError(
            "public data record registration requires the dataset to provide metadata_xml.",
        )

    details = oneprovider.get_share_details(share_id) or {}
    existing: str | None = details.get("handleId")
    if existing:
        if amend:
            metadata_details = onezone.get_handle_details(existing).get("metadata")
            metadata_matches = metadata_details == dataset.metadata_xml
            if not metadata_matches:
                onezone.update_handle(
                    handle_id=existing,
                    metadata_xml=dataset.metadata_xml,
                )
        return existing

    request_public_handle = config.record_identifier_type == "pid"
    public_handle_to_reuse = _resolve_pid_to_reuse(config.identifier_policy, dataset.pid)

    return onezone.register_handle(
        handle_service_id=config.handle_service_id,
        share_id=share_id,
        metadata_xml=dataset.metadata_xml,
        request_public_handle=request_public_handle,
        public_handle_to_reuse=public_handle_to_reuse,
    )


def _resolve_pid_to_reuse(policy: str, pid: str | None) -> str | None:
    if policy == "always-reuse-existing":
        if not pid:
            raise RecordRequirementError(
                "identifier_policy=always-reuse-existing requires dataset.pid; got none.",
            )
        return pid

    if policy == "generate-new-if-missing" and pid:
        return pid

    return None


def _join_paths(*parts: str) -> str:
    """Strip leading/trailing slashes from each part and join with `/`."""
    cleaned = [p.strip("/") for p in parts if p]
    return "/".join(cleaned)
