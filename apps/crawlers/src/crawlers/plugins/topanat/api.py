"""
TopAnat API client.

Thin façade over `HttpClient` for the EBI APIs needed to materialize the
seeded GWAS entries: GWAS REST (trait + publication metadata) and OLS
(human-readable trait descriptions). Owns no session state — pass an
open `HttpClient` and reuse it across calls.
"""

__author__ = "Bartosz Walkowicz"
__copyright__ = "Copyright (C) 2026 Onedata (onedata.org)"
__license__ = "This software is released under the MIT license cited in LICENSE.txt"

from dataclasses import dataclass
from urllib.parse import urlencode

from crawlers.core import Err, HttpClient, HttpFailure, Ok, Result
from crawlers.ui import console

# ─────────────────────────────────────────────────────────────────────────────
# Fetched info — plain data carriers returned to the parser
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TraitInfo:
    """Metadata for one EFO/MONDO trait, gathered from GWAS v1 + OLS."""

    short_form: str
    label: str
    uri: str
    description: str | None
    associations_url: str  # absolute, taken from GWAS v1 _links


@dataclass(frozen=True)
class PublicationInfo:
    """Metadata for one PubMed publication, gathered from GWAS v2."""

    pmid: str
    title: str
    journal: str | None
    publication_date: str | None  # ISO date "YYYY-MM-DD"
    authors: list[str]


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────


# The /gwas/api/search/downloads endpoint rejects requests with a 500 unless
# every filter parameter is present, even when empty. Documented quirk —
# keep the full template even if it looks noisy.
_TSV_TEMPLATE_PARAMS: tuple[tuple[str, str], ...] = (
    ("pvalfilter", ""),
    ("orfilter", ""),
    ("betafilter", ""),
    ("datefilter", ""),
    ("genomicfilter", ""),
    ("genotypingfilter[]", ""),
    ("traitfilter[]", ""),
    ("dateaddedfilter", ""),
    ("facet", "association"),
    ("efo", "true"),
)


class TopanatClient:
    """Stateless façade over `HttpClient` for the EBI GWAS REST and OLS APIs."""

    def __init__(self, http: HttpClient):
        self._http = http

    # --- GWAS metadata ---

    async def fetch_trait(self, short_form: str) -> Result[TraitInfo, HttpFailure]:
        """
        Fetch trait label/uri from GWAS v1 then enrich with description from OLS.

        Uses the v1 `/efoTraits/{id}` endpoint because v1 returns the HATEOAS
        `_links.associations` URL we want to use as the JSON file (avoids
        hand-constructing it) and works for both EFO_ and MONDO_ short forms.
        """
        result = await self._http.get_json_object(
            f"/gwas/rest/api/v2/associations?efo_id={short_form}"
        )
        if isinstance(result, Err):
            return result

        payload = result.value
        label = str(payload.get("trait") or "")
        uri = str(payload.get("uri") or "")
        associations_url = _extract_associations_link(payload) or (
            f"https://www.ebi.ac.uk/gwas/rest/api/v2/associations?efo_id={short_form}"
        )

        description = await self._fetch_ols_description(uri) if uri else None

        return Ok(
            TraitInfo(
                short_form=short_form,
                label=label,
                uri=uri,
                description=description,
                associations_url=associations_url,
            )
        )

    async def fetch_publication(self, pmid: str) -> Result[PublicationInfo, HttpFailure]:
        """Fetch publication metadata from GWAS v2 publications endpoint."""
        result = await self._http.get_json_object(f"/gwas/rest/api/v2/publications/{pmid}")
        if isinstance(result, Err):
            return result

        payload = result.value
        authors = [
            str(a.get("full_name") or "")
            for a in payload.get("authors", [])
            if isinstance(a, dict) and a.get("full_name")
        ]

        return Ok(
            PublicationInfo(
                pmid=pmid,
                title=str(payload.get("title") or ""),
                journal=_optional_str(payload.get("journal")),
                publication_date=_optional_str(payload.get("publication_date")),
                authors=authors,
            )
        )

    # --- OLS description lookup ---

    async def _fetch_ols_description(self, iri: str) -> str | None:
        """
        Look up the human description for an ontology term by IRI.

        OLS may return the same IRI under several ontologies (e.g. an EFO
        term mirrored into PRIDE); prefer the entry where
        `is_defining_ontology=true`. The endpoint never 404s on a missing
        IRI — it returns an empty `_embedded.terms` list — so a successful
        lookup with no description is a valid outcome, not an error.
        """
        url = f"/ols4/api/terms?{urlencode({'iri': iri})}"
        result = await self._http.get_json_object(url)
        if isinstance(result, Err):
            console.warning(f"OLS lookup failed for {iri}: {result.value}")
            return None

        terms = result.value.get("_embedded", {}).get("terms", [])
        if not isinstance(terms, list) or not terms:
            return None

        chosen = next(
            (t for t in terms if isinstance(t, dict) and t.get("is_defining_ontology")),
            terms[0] if isinstance(terms[0], dict) else None,
        )
        if not chosen:
            return None

        descriptions = chosen.get("description") or []
        if not isinstance(descriptions, list) or not descriptions:
            return None

        return " ".join(str(d) for d in descriptions if d)


# ─────────────────────────────────────────────────────────────────────────────
# URL builders for the two "data files" of each dataset
# ─────────────────────────────────────────────────────────────────────────────


def build_trait_tsv_url(short_form: str) -> str:
    """TSV download URL for a trait — uses GWAS' bulk-download search endpoint."""
    params = urlencode((("q", short_form), *_TSV_TEMPLATE_PARAMS))
    return f"https://www.ebi.ac.uk/gwas/api/search/downloads?{params}"


def build_publication_json_url(pmid: str) -> str:
    """JSON associations URL for a publication."""
    return f"https://www.ebi.ac.uk/gwas/rest/api/v2/associations?pubmed_id={pmid}"


def build_publication_tsv_url(pmid: str) -> str:
    """TSV download URL for a publication — note `pubmedId:` prefix in the query."""
    params = urlencode((("q", f"pubmedId:{pmid}"), *_TSV_TEMPLATE_PARAMS))
    return f"https://www.ebi.ac.uk/gwas/api/search/downloads?{params}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _extract_associations_link(payload: dict) -> str | None:
    """Pull the canonical associations URL out of a GWAS HATEOAS response."""
    links = payload.get("_links")
    if not isinstance(links, dict):
        return None
    associations = links.get("associations")
    if not isinstance(associations, dict):
        return None
    href = associations.get("href")
    return str(href) if href else None


def _optional_str(value: object) -> str | None:
    """Return `str(value)` for non-empty values, else None."""
    if value is None or value == "":
        return None
    return str(value)
