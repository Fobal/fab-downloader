"""Fab library and download endpoints, the ones the Epic Games Launcher uses.

Fab has no public API.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from .net import HttpClient, HttpError

if TYPE_CHECKING:
    from .auth import Session

FAB_HOST = "https://www.fab.com"
_ENGINE_VERSION_RE = re.compile(r"(?:UE_?)?(\d+)\.(\d+)", re.IGNORECASE)


class FabError(Exception):
    """Fab returned something this tool cannot use."""


@dataclass(frozen=True)
class ProjectVersion:
    artifact_id: str
    engine_versions: tuple[str, ...]


@dataclass(frozen=True)
class LibraryItem:
    asset_id: str
    namespace: str
    title: str
    distribution_method: str
    project_versions: tuple[ProjectVersion, ...]

    @property
    def engine_versions(self) -> list[str]:
        versions = {version for project in self.project_versions for version in project.engine_versions}
        return sorted(versions, key=engine_sort_key)

    def version_for(self, engine_version: str) -> ProjectVersion | None:
        return next((p for p in self.project_versions if engine_version in p.engine_versions), None)


@dataclass(frozen=True)
class DownloadSource:
    manifest_url: str
    base_url: str


@dataclass(frozen=True)
class Build:
    """The current build of an artifact and where to download it."""

    build_version: str
    sources: list[DownloadSource]


def normalize_engine_version(text: str) -> str:
    """Turn 5.3, UE5.3 or UE_5.3 into Fab's UE_5.3."""
    match = _ENGINE_VERSION_RE.fullmatch(text.strip())
    if not match:
        raise ValueError(f"Engine version must look like 5.3, not {text!r}")
    return f"UE_{int(match[1])}.{int(match[2])}"


def engine_sort_key(version: str) -> tuple[int, int]:
    match = _ENGINE_VERSION_RE.fullmatch(version)
    return (int(match[1]), int(match[2])) if match else (-1, -1)


def parse_library_item(raw: dict) -> LibraryItem:
    versions = tuple(
        ProjectVersion(project["artifactId"], tuple(project.get("engineVersions") or ()))
        for project in raw.get("projectVersions") or ()
    )
    return LibraryItem(
        asset_id=raw["assetId"],
        namespace=raw["assetNamespace"],
        title=raw.get("title") or raw["assetId"],
        distribution_method=raw.get("distributionMethod") or "",
        project_versions=versions,
    )


def list_library(http: HttpClient, session: Session) -> list[LibraryItem]:
    items: list[LibraryItem] = []
    cursor = None
    while True:
        query = {"count": 100} if cursor is None else {"cursor": cursor, "count": 100}
        url = f"{FAB_HOST}/e/accounts/{quote(session.account_id, safe='')}/ue/library?{urlencode(query)}"
        page = http.json("GET", url, headers=_auth_headers(session))
        items.extend(parse_library_item(raw) for raw in page.get("results") or ())
        cursor = (page.get("cursors") or {}).get("next")
        if not cursor:
            return items


def find_items(items: list[LibraryItem], search: str) -> list[LibraryItem]:
    """Match an exact asset id, otherwise every title that contains the search text."""
    exact = [item for item in items if item.asset_id == search]
    if exact:
        return exact
    needle = search.casefold()
    return [item for item in items if needle in item.title.casefold()]


def request_build(
    http: HttpClient,
    session: Session,
    item: LibraryItem,
    version: ProjectVersion,
    platform: str = "Windows",
) -> Build:
    """Ask Fab for the artifact's current build and where its manifest and chunks can be downloaded."""
    url = f"{FAB_HOST}/e/artifacts/{quote(version.artifact_id, safe='')}/manifest"
    body = json.dumps({"item_id": item.asset_id, "namespace": item.namespace, "platform": platform}).encode()
    try:
        data = http.json(
            "POST", url, headers={**_auth_headers(session), "Content-Type": "application/json"}, body=body
        )
    except HttpError as error:
        if error.status == 403:
            raise FabError(
                "Fab answered HTTP 403 to the download request. Other Fab clients treat this "
                "as a temporary limit; wait a minute and try again."
            ) from None
        raise

    infos = data.get("downloadInfo") or []
    info = next((i for i in infos if i.get("artifactId") == version.artifact_id), infos[0] if infos else None)
    if info is None:
        raise FabError("Fab returned no download information for this build.")
    return Build(info.get("buildVersion") or "", pair_sources(info))


def pair_sources(info: dict) -> list[DownloadSource]:
    """Match each CDN base URL with the signed manifest URL on the same CDN."""
    manifest_urls = [point.get("manifestUrl") or "" for point in info.get("distributionPoints") or ()]
    sources = []
    for base_url in info.get("distributionPointBaseUrls") or ():
        manifest_url = next((url for url in manifest_urls if url.startswith(base_url)), None)
        if manifest_url:
            sources.append(DownloadSource(manifest_url, base_url.rstrip("/")))
    if not sources:
        raise FabError("Fab returned no usable download location for this build.")
    return sources


def _auth_headers(session: Session) -> dict[str, str]:
    return {"Authorization": f"Bearer {session.access_token}"}
