"""
TopAnat parser.

Maps a fetched `TraitInfo` / `PublicationInfo` (plus the two known data-file
URLs) into an `OnedataDataset` carrying a complete DataCite metadata payload.
Pure mapping — no I/O, no HTTP — so it can be unit-tested in isolation
and the plugin file stays focused on lifecycle wiring.
"""

__author__ = "Bartosz Walkowicz"
__copyright__ = "Copyright (C) 2026 Onedata (onedata.org)"
__license__ = "This software is released under the MIT license cited in LICENSE.txt"

from datetime import UTC, datetime

from crawlers.core.dataset import OnedataDataset, OnedataFile
from crawlers.metadata.datacite import (
    Creator,
    DataCiteRecord,
    Date,
    DateType,
    Description,
    IdentifierType,
    NameType,
    RelatedIdentifier,
    RelatedIdentifierType,
    RelationType,
    Rights,
)
from crawlers.plugins.topanat.api import (
    PublicationInfo,
    TraitInfo,
    build_publication_json_url,
    build_publication_tsv_url,
    build_trait_tsv_url,
)
from crawlers.plugins.utils.datetime import year_from_iso

# ─────────────────────────────────────────────────────────────────────────────
# Shared defaults — applied uniformly across all TopAnat datasets
# ─────────────────────────────────────────────────────────────────────────────

_CREATOR_NAME = "GWAS Catalog team"
_PUBLISHER = "GWAS Catalog team"
_RESOURCE_TYPE_GENERAL = "Dataset"
_RESOURCE_TYPE_VALUE = "GWAS associations"
_SUBJECTS = ["GWAS", "genome-wide association study"]
_RIGHTS = Rights(
    text="EBI Terms of Use",
    uri="https://www.ebi.ac.uk/about/terms-of-use/",
)
_DESCRIPTION_NOTE = (
    "NOTE: The data is queried live from the GWAS server whenever read via "
    "Onedata. The query may take a couple of minutes to be processed, "
    "despite small result file sizes."
)

# ─────────────────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────────────────


def build_trait_record(info: TraitInfo) -> OnedataDataset:
    """Build an `OnedataDataset` for a fetched EFO/MONDO trait."""
    identifier = f"https://www.ebi.ac.uk/gwas/efotraits/{info.short_form}"
    title = f"Trait: {info.label}" if info.label else f"Trait: {info.short_form}"

    today = datetime.now(UTC).strftime("%Y-%m-%d")

    descriptions: list[Description] = []
    if info.description:
        descriptions.append(Description(value=info.description + f"\n\n{_DESCRIPTION_NOTE}"))

    related: list[RelatedIdentifier] = []
    if info.uri:
        related.append(
            RelatedIdentifier(
                value=info.uri,
                identifier_type=RelatedIdentifierType.URL,
                relation_type=RelationType.IS_REFERENCED_BY,
            )
        )

    record = DataCiteRecord(
        identifier=identifier,
        identifier_type=IdentifierType.URL,
        creators=[Creator(name=_CREATOR_NAME, name_type=NameType.ORGANIZATIONAL)],
        title=title,
        publisher=_PUBLISHER,
        # Traits have no natural publication year — they are live curated
        # entries. Use today's year and tag the date as Updated to make the
        # "snapshot at crawl time" semantics explicit.
        publication_year=datetime.now(UTC).year,
        resource_type_general=_RESOURCE_TYPE_GENERAL,
        resource_type_value=_RESOURCE_TYPE_VALUE,
        subjects=_SUBJECTS + ([info.label] if info.label else []),
        dates=[Date(value=today, date_type=DateType.UPDATED)],
        descriptions=descriptions,
        related_identifiers=related,
        rights_list=[_RIGHTS],
    )

    return OnedataDataset(
        name=title,
        target_dir=title.replace("/", "-"),
        # This repository doesn't provide Persistent Identifiers for its datasets
        pid=None,
        metadata_xml=record.to_xml(),
        files=(
            OnedataFile(path="data.json", url=info.associations_url),
            OnedataFile(path="data.tsv", url=build_trait_tsv_url(info.short_form)),
        ),
    )


def build_publication_record(info: PublicationInfo) -> OnedataDataset:
    """Build an `OnedataDataset` for a fetched GWAS publication."""
    pid = f"https://www.ebi.ac.uk/gwas/publications/{info.pmid}"
    title = info.title or f"Publication: {info.pmid}"

    creators: list[Creator] = [
        Creator(name=author, name_type=NameType.PERSONAL) for author in info.authors
    ] or [Creator(name=_CREATOR_NAME, name_type=NameType.ORGANIZATIONAL)]

    dates: list[Date] = []
    if info.publication_date:
        dates.append(Date(value=info.publication_date, date_type=DateType.ISSUED))

    # PubMed cross-reference — useful for downstream consumers that want to
    # round-trip back to the publication record.
    related = [
        RelatedIdentifier(
            value=f"https://pubmed.ncbi.nlm.nih.gov/{info.pmid}",
            identifier_type=RelatedIdentifierType.URL,
            relation_type=RelationType.IS_SUPPLEMENT_TO,
        )
    ]

    record = DataCiteRecord(
        identifier=pid,
        identifier_type=IdentifierType.URL,
        creators=creators,
        title=title,
        publisher=_PUBLISHER,
        publication_year=year_from_iso(info.publication_date),
        resource_type_general=_RESOURCE_TYPE_GENERAL,
        resource_type_value=_RESOURCE_TYPE_VALUE,
        subjects=_SUBJECTS,
        dates=dates,
        descriptions=(
            [Description(value=f"Published in {info.journal}.\n\n{_DESCRIPTION_NOTE}")]
            if info.journal
            else [Description(value=_DESCRIPTION_NOTE)]
        ),
        related_identifiers=related,
        rights_list=[_RIGHTS],
    )

    return OnedataDataset(
        name=title,
        target_dir=title.replace("/", "-"),
        pid=pid,
        metadata_xml=record.to_xml(),
        files=(
            OnedataFile(path="data.json", url=build_publication_json_url(info.pmid)),
            OnedataFile(path="data.tsv", url=build_publication_tsv_url(info.pmid)),
        ),
    )
