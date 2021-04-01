"""link helpers."""

from abc import get_cache_token
from typing import Dict, List, Optional, Union
from urllib.parse import quote, urljoin
from urllib.parse import urlparse, ParseResult, parse_qs, urlencode, unquote

from pathlib import Path
from starlette.requests import Request

import attr
from stac_pydantic.shared import Link, MimeTypes, Relations
from stac_pydantic.api.extensions.paging import PaginationLink


from stac_fastapi.extensions.third_party.tiles import OGCTileLink

import logging

logger = logging.getLogger("uvicorn")
logger.setLevel(logging.INFO)

# These can be inferred from the item/collection so they aren't included in the database
# Instead they are dynamically generated when querying the database using the classes defined below
INFERRED_LINK_RELS = ["self", "item", "parent", "collection", "root"]


def filter_links(links: List[Dict]) -> List[Dict]:
    """Remove inferred links."""
    return [link for link in links if link["rel"] not in INFERRED_LINK_RELS]


def merge_params(url: str, newparams: Dict) -> str:
    u = urlparse(url)
    params = parse_qs(u.query)
    logger.info(f'oldparams: {params} newparams: {newparams}')
    params.update(newparams)
    logger.info(f'updated params: {params}')
    param_string = unquote(urlencode(params, True))
    logger.info(f'param_string: {param_string}')

    href = ParseResult(
        scheme=u.scheme,
        netloc=u.netloc,
        path=u.path,
        params=u.params,
        query=param_string,
        fragment=u.fragment,
    ).geturl()
    return href


@attr.s
class BaseLinks:
    """Create inferred links common to collections and items."""

    request: Request = attr.ib()

    @property
    def base_url(self):
        return str(self.request.base_url)

    @property
    def url(self):
        return str(self.request.url)

    def resolve(self, url):
        return urljoin(str(self.base_url), str(url))

    def link_self(self) -> Link:
        """Return the self link."""
        return Link(rel=Relations.self, type=MimeTypes.json, href=self.url)

    def link_root(self) -> Link:
        """Return the catalog root."""
        return Link(
            rel=Relations.root, type=MimeTypes.json, href=self.base_url
        )

    def create_links(self) -> List[Union[PaginationLink, Link]]:
        """Return all inferred links."""
        links = []
        for name in dir(self):
            if name.startswith("link_") and callable(getattr(self, name)):
                link = getattr(self, name)()
                if link is not None:
                    links.append(link)
        return links

    async def get_links(
        self, extra_links: List[Union[PaginationLink, Link]] = []
    ) -> List[Union[PaginationLink, Link]]:
        if self.request.method == "POST":
            self.request.postbody = await self.request.json()
        # join passed in links with generated links
        # and update relative paths
        links = self.create_links()
        logger.info(f'get_links before checking extra {links}')
        if extra_links is not None and len(extra_links) >= 1:
            logger.info(f'extra links orig: {extra_links}')
            for link in extra_links:
                if link.rel not in INFERRED_LINK_RELS:
                    link.href=self.resolve(link.href)
                    links.append(link)
            logger.info(f'after extra links: {links}')
        logger.info(f"links: {links}")
        return links


@attr.s
class PagingLinks(BaseLinks):

    next: str = attr.ib(kw_only=True, default=None)
    prev: str = attr.ib(kw_only=True, default=None)

    def link_next(self) -> PaginationLink:
        if self.next is not None:
            method = self.request.method
            if method == "GET":
                href=merge_params(self.url, {'token':f"next:{self.next}"})
                link = PaginationLink(
                    rel=Relations.next,
                    type=MimeTypes.json,
                    method=method,
                    href=href,
                )
                logger.info(link)
                return link
            if method == "POST":
                body = self.request.postbody
                body["token"] = f"prev:{self.next}"
                return PaginationLink(
                    rel=Relations.next,
                    type=MimeTypes.json,
                    method=method,
                    href=f"{self.request.url}",
                    body=body,
                )

    def link_prev(self) -> PaginationLink:
        if self.prev is not None:
            method = self.request.method
            if method == "GET":
                href=merge_params(self.url, {'token':f"prev:{self.prev}"})
                return PaginationLink(
                    rel=Relations.previous,
                    type=MimeTypes.json,
                    method=method,
                    href=href,
                )
            if method == "POST":
                body = self.request.postbody
                body["token"] = f"prev:{self.prev}"
                return PaginationLink(
                    rel=Relations.previous,
                    type=MimeTypes.json,
                    method=method,
                    href=f"{self.request.url}",
                    body=body,
                )


@attr.s
class CollectionLinksBase(BaseLinks):
    """Create inferred links specific to collections."""

    collection_id: str = attr.ib()

    def collection_link(self, rel=Relations.collection) -> Link:
        return Link(
            rel=rel,
            type=MimeTypes.json,
            href=self.resolve(f"/collections/{self.collection_id}"),
        )


@attr.s
class CollectionLinks(CollectionLinksBase):
    """Create inferred links specific to collections."""

    def link_self(self) -> Link:
        """Return the self link."""
        return self.collection_link(rel=Relations.self)

    def link_parent(self) -> Link:
        """Create the `parent` link."""
        return Link(
            rel=Relations.parent,
            type=MimeTypes.json,
            href=urljoin(self.base_url, "/"),
        )

    def link_item(self) -> Link:
        """Create the `item` link."""
        return Link(
            rel=Relations.item,
            type=MimeTypes.geojson,
            href=self.resolve(f"/collections/{self.collection_id}/items"),
        )


@attr.s
class ItemLinks(CollectionLinksBase):
    """Create inferred links specific to items."""

    item_id: str = attr.ib()

    def link_self(self) -> Link:
        return Link(
            rel=Relations.self,
            type=MimeTypes.geojson,
            href=self.resolve(
                f"/collections/{self.collection_id}/items/{self.item_id}"
            ),
        )

    def link_parent(self) -> Link:
        """Create the `parent` link."""
        return self.collection_link(rel=Relations.parent)

    def link_collection(self) -> Link:
        """Create the `collection` link."""
        return self.collection_link()

    def link_tiles(self) -> Link:
        """Create the `tiles` link."""
        return Link(
            rel=Relations.alternate,
            type=MimeTypes.json,
            title="tiles",
            href=self.resolve(
                f"/collections/{self.collection_id}/items/{self.item_id}/tiles",
            ),
        )


@attr.s
class TileLinks:
    """Create inferred links specific to OGC Tiles API."""

    base_url: str = attr.ib()
    collection_id: str = attr.ib()
    item_id: str = attr.ib()

    def __post_init__(self):
        """Post init handler."""
        self.item_uri = urljoin(
            self.base_url,
            f"/collections/{self.collection_id}/items/{self.item_id}",
        )

    def tiles(self) -> OGCTileLink:
        """Create tiles link."""
        return OGCTileLink(
            href=urljoin(
                self.base_url,
                f"/titiler/tiles/{{z}}/{{x}}/{{y}}.png?url={self.item_uri}",
            ),
            rel=Relations.item,
            title="tiles",
            type=MimeTypes.png,
            templated=True,
        )

    def viewer(self) -> OGCTileLink:
        """Create viewer link."""
        return OGCTileLink(
            href=urljoin(
                self.base_url, f"/titiler/viewer?url={self.item_uri}"
            ),
            rel=Relations.alternate,
            type=MimeTypes.html,
            title="viewer",
        )

    def tilejson(self) -> OGCTileLink:
        """Create tilejson link."""
        return OGCTileLink(
            href=urljoin(
                self.base_url, f"/titiler/tilejson.json?url={self.item_uri}"
            ),
            rel=Relations.alternate,
            type=MimeTypes.json,
            title="tilejson",
        )

    def wmts(self) -> OGCTileLink:
        """Create wmts capabilities link."""
        return OGCTileLink(
            href=urljoin(
                self.base_url,
                f"/titiler/WMTSCapabilities.xml?url={self.item_uri}",
            ),
            rel=Relations.alternate,
            type=MimeTypes.xml,
            title="WMTS Capabilities",
        )

    def create_links(self) -> List[OGCTileLink]:
        """Return all inferred links."""
        return [self.tiles(), self.tilejson(), self.wmts(), self.viewer()]
