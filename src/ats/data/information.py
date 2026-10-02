"""Policy-gated information collection; source text is data, never instructions."""

import hashlib
import importlib
import io
import ipaddress
import json
import re
import threading
import time
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Annotated, Any, Literal, Self, cast
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import Element

import httpx
from pydantic import (
    AwareDatetime,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from ats.data.storage import LocalPayloadStore, StoragePermit, StoredPayload
from ats.domain.governance import evidence_digest
from ats.domain.policy import SourceAllowlist
from ats.domain.prices import SEOUL
from ats.domain.strategy import ArtifactRef, FrozenModel, Identifier, Sha256Digest

_OBJECT = TypeAdapter(dict[str, Any])


class InformationError(ValueError):
    pass


class InformationSource(FrozenModel):
    source_id: Identifier
    endpoint: str
    format: Literal["DART", "RSS", "ATOM", "NAVER_NEWS", "KRX_JSON", "TELEGRAM_EXPORT"]
    category: Literal["DISCLOSURE", "NEWS", "PUBLIC_RUMOR", "MARKET"]
    terms: ArtifactRef
    allow_collection: bool = False
    allowed_scope: Annotated[tuple[str, ...], Field(min_length=1)]
    max_pages: Annotated[int, Field(strict=True, ge=1, le=100)] = 10
    max_bytes: Annotated[int, Field(strict=True, ge=1, le=4 * 1024 * 1024)] = (
        1024 * 1024
    )
    max_items: Annotated[int, Field(strict=True, ge=1, le=1000)] = 100

    @model_validator(mode="after")
    def fixed_public_url(self) -> Self:
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
            or "." not in parsed.hostname
            or parsed.hostname.endswith((".local", ".internal"))
        ):
            raise ValueError(
                "an exact public HTTPS endpoint without embedded credentials is required"
            )
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise ValueError("IP-literal sources are forbidden")
        if (
            self.format == "DART"
            and self.endpoint != "https://opendart.fss.or.kr/api/list.json"
        ):
            raise ValueError("DART list endpoint is fixed")
        if (
            self.format == "NAVER_NEWS"
            and self.endpoint != "https://openapi.naver.com/v1/search/news.json"
        ):
            raise ValueError("Naver news endpoint is fixed")
        return self


class TextObservation(FrozenModel):
    source_id: Identifier
    item_id: Identifier
    revision: Sha256Digest
    observed_at: AwareDatetime
    published_at: AwareDatetime | None = None
    published_date: date | None = None
    updated_at: AwareDatetime | None = None
    category: Literal["DISCLOSURE", "NEWS", "PUBLIC_RUMOR"]
    title: Annotated[str, Field(min_length=1, max_length=4096)]
    text: Annotated[str, Field(max_length=128 * 1024)]
    url: str
    company_id: str | None = None
    symbol: str | None = None
    correction: bool = False
    supersedes: Identifier | None = None
    retraction: bool = False
    raw: StoredPayload
    policy_digest: Sha256Digest
    discovered_at: AwareDatetime | None = None
    origin_raw: StoredPayload | None = None
    publication_basis: Literal["SOURCE", "PROVIDER", "UNKNOWN"] = "SOURCE"

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if (
            self.raw.source_id != self.source_id
            or self.raw.policy_digest != self.policy_digest
            or self.observed_at != self.raw.observed_at
            or any(
                stamp > self.observed_at
                for stamp in (self.published_at, self.updated_at)
                if stamp is not None
            )
            or self.published_date is not None
            and self.published_date > self.observed_at.astimezone(SEOUL).date()
        ):
            raise ValueError("information availability or source provenance mismatch")
        if self.discovered_at is not None and self.discovered_at > self.observed_at:
            raise ValueError("discovery cannot follow text observation")
        return self


class InformationBatch(FrozenModel):
    source_digest: Sha256Digest
    observations: tuple[TextObservation, ...]
    pages: tuple[StoredPayload, ...]
    observed_at: AwareDatetime
    declared_count: Annotated[int, Field(strict=True, ge=0)]
    complete: Literal[True] = True
    coverage: Literal["REQUEST_RANGE", "FEED_WINDOW", "SEARCH_WINDOW"] = "REQUEST_RANGE"
    requested_start: date | None = None
    requested_end: date | None = None

    @model_validator(mode="after")
    def exact_coverage(self) -> Self:
        keys = [(item.source_id, item.item_id) for item in self.observations]
        if len(keys) != len(set(keys)) or len(keys) != self.declared_count:
            raise ValueError("information batch has duplicate or missing items")
        if any(
            item.raw not in self.pages or item.observed_at > self.observed_at
            for item in self.observations
        ):
            raise ValueError("item is not backed by a retained batch page")
        return self


class InformationClient:
    def __init__(
        self,
        source: InformationSource,
        policy: SourceAllowlist,
        permit: StoragePermit,
        store: LocalPayloadStore,
        *,
        transport: httpx.BaseTransport | None = None,
        allow_network: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.source = InformationSource.model_validate(source.model_dump())
        self.policy, self.permit, self.store = policy, permit, store
        self.clock = clock or (lambda: datetime.now(UTC))
        if not allow_network and not isinstance(transport, httpx.MockTransport):
            raise InformationError("real information collection is disarmed")
        self.mock = isinstance(transport, httpx.MockTransport)
        self.client = httpx.Client(
            transport=transport, timeout=10, follow_redirects=False, trust_env=False
        )
        self.lock = threading.Lock()
        self.last_request = 0.0

    def close(self) -> None:
        self.client.close()

    def authorize(self) -> int:
        if (
            not self.source.allow_collection
            or self.source.source_id != self.permit.source_id
        ):
            raise InformationError("source collection scope is not approved")
        self.store.check_authorization(
            now=self.clock(), source_policy=self.policy, permit=self.permit
        )
        entry = next(
            entry
            for entry in self.policy.sources
            if entry.source_id == self.source.source_id
        )
        if entry.rate_limit_per_minute is None:
            raise InformationError("explicit source rate required")
        return entry.rate_limit_per_minute

    def fetch(
        self,
        parameters: dict[str, str],
        *,
        dart_document: bool = False,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, datetime]:
        rate = self.authorize()
        endpoint = self.source.endpoint
        if dart_document:
            if (
                self.source.format != "DART"
                or "original-documents" not in self.source.allowed_scope
            ):
                raise InformationError("original document scope is not approved")
            endpoint = "https://opendart.fss.or.kr/api/document.xml"
        with self.lock:
            delay = self.last_request + 60 / rate - time.monotonic()
            if delay > 0 and not self.mock:
                time.sleep(delay)
            self.last_request = time.monotonic()
            try:
                with self.client.stream(
                    "GET", endpoint, params=parameters, headers=headers
                ) as response:
                    if response.status_code != 200:
                        raise InformationError(
                            "source HTTP failure; batch not complete"
                        )
                    raw = bytearray()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        raw.extend(chunk)
                        if len(raw) > self.source.max_bytes:
                            raise InformationError(
                                "source response exceeds byte budget"
                            )
                    return bytes(raw), self.clock()
            except httpx.HTTPError:
                raise InformationError(
                    "source transport failed; no complete batch"
                ) from None

    def retain(self, raw: bytes, observed_at: datetime) -> StoredPayload:
        return self.store.put(
            raw,
            observed_at=observed_at,
            now=self.clock(),
            source_policy=self.policy,
            permit=self.permit,
        )


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InformationError("duplicate JSON fields")
        result[key] = value
    return result


def _integer(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise InformationError("invalid source count")


def collect_dart(
    client: InformationClient,
    *,
    start: date,
    end: date,
    key: SecretStr,
    include_documents: bool = False,
) -> InformationBatch:
    if (
        client.source.format != "DART"
        or start > end
        or (end - start).days > 89
        or end > client.clock().astimezone(SEOUL).date()
    ):
        raise InformationError("DART requires a bounded nonfuture date range")
    if "all-filings" not in client.source.allowed_scope or not key.get_secret_value():
        raise InformationError("DART scope or key unavailable")
    if include_documents and "original-documents" not in client.source.allowed_scope:
        raise InformationError("original document scope is not approved")
    observations: list[TextObservation] = []
    pages: list[StoredPayload] = []
    expected: tuple[int, int] | None = None
    for number in range(1, client.source.max_pages + 1):
        raw, observed = client.fetch(
            {
                "crtfc_key": key.get_secret_value(),
                "bgn_de": start.strftime("%Y%m%d"),
                "end_de": end.strftime("%Y%m%d"),
                "last_reprt_at": "N",
                "sort": "date",
                "sort_mth": "asc",
                "page_count": "100",
                "page_no": str(number),
            }
        )
        try:
            body = _OBJECT.validate_python(json.loads(raw, object_pairs_hook=_unique))
            if body.get("status") == "013" and number == 1:
                receipt = client.retain(raw, observed)
                return InformationBatch(
                    source_digest=evidence_digest(client.source),
                    observations=(),
                    pages=(receipt,),
                    declared_count=0,
                    observed_at=observed,
                    requested_start=start,
                    requested_end=end,
                )
            if body.get("status") != "000":
                raise InformationError("DART response refused; no watermark advance")
            total, count = _integer(body["total_page"]), _integer(body["total_count"])
            if (
                total < 1
                or count > client.source.max_items
                or total > client.source.max_pages
                or _integer(body["page_no"]) != number
                or _integer(body["page_count"]) != 100
                or total != (count + 99) // 100
                or expected is not None
                and expected != (total, count)
            ):
                raise InformationError("DART pagination changed or exceeds budget")
            expected = (total, count)
            rows = TypeAdapter(list[dict[str, str]]).validate_python(body["list"])
            if len(rows) != (100 if number < total else count - 100 * (total - 1)):
                raise InformationError("DART page is incomplete")
            receipt = client.retain(raw, observed)
            pages.append(receipt)
            for row in rows:
                received = datetime.strptime(row["rcept_dt"], "%Y%m%d").date()
                if not start <= received <= end:
                    raise InformationError("DART item outside requested range")
                title, item_id = row["report_nm"], row["rcept_no"]
                if len(item_id) != 14 or not item_id.isascii() or not item_id.isdigit():
                    raise InformationError("invalid DART receipt identity")
                encoded = json.dumps(row, ensure_ascii=True, sort_keys=True).encode()
                observations.append(
                    TextObservation(
                        source_id=client.source.source_id,
                        item_id=item_id,
                        revision="sha256:" + hashlib.sha256(encoded).hexdigest(),
                        observed_at=receipt.observed_at,
                        published_date=received,
                        category="DISCLOSURE",
                        title=title,
                        text=title,
                        url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item_id}",
                        company_id=row["corp_code"],
                        symbol=row.get("stock_code") or None,
                        correction="정정" in title,
                        raw=receipt,
                        policy_digest=evidence_digest(client.policy),
                    )
                )
            if number == total:
                if include_documents:
                    if len(observations) > client.source.max_items:
                        raise InformationError("document request budget exceeded")
                    observations = [
                        collect_dart_document(client, item, key=key)
                        for item in observations
                    ]
                    pages.extend(item.raw for item in observations)
                    observed = client.clock()
                return InformationBatch(
                    source_digest=evidence_digest(client.source),
                    observations=tuple(observations),
                    pages=tuple(pages),
                    declared_count=count,
                    observed_at=observed,
                    requested_start=start,
                    requested_end=end,
                )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise InformationError(
                "DART response validation failed; no complete batch"
            ) from None
    raise InformationError("DART page budget exhausted")


def _xml(raw: bytes) -> Element:
    try:
        parser = importlib.import_module("defusedxml.ElementTree")
        return cast(
            Element,
            parser.fromstring(
                raw, forbid_dtd=True, forbid_entities=True, forbid_external=True
            ),
        )
    except Exception:
        raise InformationError("unsafe or malformed source XML") from None


def collect_dart_document(
    client: InformationClient, item: TextObservation, *, key: SecretStr
) -> TextObservation:
    if item.source_id != client.source.source_id or item.category != "DISCLOSURE":
        raise InformationError("original document source mismatch")
    raw, observed = client.fetch(
        {"crtfc_key": key.get_secret_value(), "rcept_no": item.item_id},
        dart_document=True,
    )
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (
                not entries
                or len(entries) > 16
                or sum(entry.file_size for entry in entries) > client.source.max_bytes
                or any(
                    entry.is_dir()
                    or "/" in entry.filename
                    or "\\" in entry.filename
                    or ".." in entry.filename
                    or not entry.filename.lower().endswith(".xml")
                    or entry.flag_bits & 1
                    or entry.file_size > max(1, entry.compress_size) * 200
                    for entry in entries
                )
            ):
                raise InformationError(
                    "unsafe document archive or expansion budget exceeded"
                )
            texts: list[str] = []
            for entry in entries:
                with archive.open(entry) as stream:
                    document = stream.read(client.source.max_bytes + 1)
                    if len(document) > client.source.max_bytes:
                        raise InformationError("document exceeds expansion budget")
                root = _xml(document)
                texts.append(" ".join(root.itertext()).strip())
    except (zipfile.BadZipFile, RuntimeError, OSError):
        raise InformationError("invalid DART document archive") from None
    text = "\n".join(texts)
    receipt = client.retain(raw, observed)
    return TextObservation.model_validate(
        {
            **item.model_dump(),
            "text": text,
            "revision": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
            "raw": receipt,
            "origin_raw": item.raw,
            "discovered_at": item.observed_at,
            "observed_at": receipt.observed_at,
        }
    )


def collect_feed(client: InformationClient) -> InformationBatch:
    if client.source.category == "MARKET":
        raise InformationError("market reference sources are not text feeds")
    if client.source.format not in ("RSS", "ATOM") or client.source.allowed_scope != (
        client.source.endpoint,
    ):
        raise InformationError("feed scope must name the exact approved endpoint")
    raw, observed = client.fetch({})
    root = _xml(raw)
    atom = "{http://www.w3.org/2005/Atom}"
    if client.source.format == "RSS":
        if root.tag != "rss" or root.find("channel") is None:
            raise InformationError("not an RSS 2.0 feed")
        entries = root.findall("channel/item")
    else:
        if (
            root.tag != atom + "feed"
            or root.find(f"{atom}link[@rel='next']") is not None
        ):
            raise InformationError("unsupported Atom root or incomplete paginated feed")
        entries = root.findall(atom + "entry")
    if len(entries) > client.source.max_items:
        raise InformationError("feed item budget exceeded; no truncation")
    receipt = client.retain(raw, observed)
    items: list[TextObservation] = []
    for entry in entries:
        if client.source.format == "RSS":
            identity = entry.findtext("guid") or entry.findtext("link")
            title = entry.findtext("title") or ""
            text = entry.findtext("description") or ""
            url = urljoin(client.source.endpoint, entry.findtext("link") or "")
            publication = entry.findtext("pubDate")
            published = parsedate_to_datetime(publication) if publication else None
            updated = None
        else:
            identity = entry.findtext(atom + "id")
            title = entry.findtext(atom + "title") or ""
            content = entry.find(atom + "content")
            if content is not None and content.get("src"):
                raise InformationError(
                    "external Atom content is outside approved scope"
                )
            text = (
                " ".join(content.itertext())
                if content is not None
                else entry.findtext(atom + "summary") or ""
            )
            link = next(
                (
                    link
                    for link in entry.findall(atom + "link")
                    if link.get("rel", "alternate") == "alternate"
                ),
                None,
            )
            url = urljoin(
                client.source.endpoint, link.get("href", "") if link is not None else ""
            )
            published = (
                datetime.fromisoformat(
                    entry.findtext(atom + "published", "").replace("Z", "+00:00")
                )
                if entry.findtext(atom + "published")
                else None
            )
            updated = datetime.fromisoformat(
                entry.findtext(atom + "updated", "").replace("Z", "+00:00")
            )
        if not identity or urlsplit(url).scheme not in ("http", "https"):
            raise InformationError("feed item lacks stable identity or safe citation")
        material = {
            "title": title,
            "text": text,
            "url": url,
            "updated": updated.isoformat() if updated else None,
        }
        items.append(
            TextObservation(
                source_id=client.source.source_id,
                item_id=hashlib.sha256(identity.encode()).hexdigest(),
                revision="sha256:"
                + hashlib.sha256(
                    json.dumps(material, sort_keys=True).encode()
                ).hexdigest(),
                observed_at=receipt.observed_at,
                published_at=published,
                updated_at=updated,
                category=client.source.category,
                title=title,
                text=text,
                url=url,
                raw=receipt,
                policy_digest=evidence_digest(client.policy),
            )
        )
    return InformationBatch(
        source_digest=evidence_digest(client.source),
        observations=tuple(items),
        pages=(receipt,),
        observed_at=observed,
        declared_count=len(items),
        coverage="FEED_WINDOW",
    )


def collect_naver_news(
    client: InformationClient, *, query: str, client_id: SecretStr, secret: SecretStr
) -> InformationBatch:
    if client.source.format != "NAVER_NEWS" or query not in client.source.allowed_scope:
        raise InformationError("news query is not in approved scope")
    pages: list[StoredPayload] = []
    items: list[TextObservation] = []
    expected: int | None = None
    for index in range(min(client.source.max_pages, 10)):
        start = index * 100 + 1
        raw, observed = client.fetch(
            {"query": query, "start": str(start), "display": "100", "sort": "date"},
            headers={
                "X-Naver-Client-Id": client_id.get_secret_value(),
                "X-Naver-Client-Secret": secret.get_secret_value(),
            },
        )
        try:
            body = _OBJECT.validate_python(json.loads(raw, object_pairs_hook=_unique))
            total = _integer(body["total"])
            if (
                expected is not None
                and expected != total
                or _integer(body["start"]) != start
            ):
                raise InformationError("search result moved during pagination")
            expected = total
            rows = TypeAdapter(list[dict[str, str]]).validate_python(body["items"])
            if len(rows) != min(100, max(0, total - start + 1)) or len(
                rows
            ) != _integer(body["display"]):
                raise InformationError("search result page is incomplete")
            receipt = client.retain(raw, observed)
            pages.append(receipt)
            for row in rows:
                url = row["originallink"] or row["link"]
                if urlsplit(url).scheme not in ("http", "https"):
                    raise InformationError("unsafe news citation")
                items.append(
                    TextObservation(
                        source_id=client.source.source_id,
                        item_id=hashlib.sha256(url.encode()).hexdigest(),
                        revision="sha256:"
                        + hashlib.sha256(
                            json.dumps(row, sort_keys=True).encode()
                        ).hexdigest(),
                        observed_at=receipt.observed_at,
                        published_at=parsedate_to_datetime(row["pubDate"]),
                        publication_basis="PROVIDER",
                        category="NEWS",
                        title=row["title"],
                        text=row["description"],
                        url=url,
                        raw=receipt,
                        policy_digest=evidence_digest(client.policy),
                    )
                )
            if len(items) > client.source.max_items:
                raise InformationError("news item budget exceeded")
            if start + len(rows) > total:
                return InformationBatch(
                    source_digest=evidence_digest(client.source),
                    observations=tuple(items),
                    pages=tuple(pages),
                    observed_at=observed,
                    declared_count=len(items),
                    coverage="SEARCH_WINDOW",
                )
        except (ValueError, KeyError, TypeError):
            raise InformationError("news response invalid or incomplete") from None
    raise InformationError("news page budget exhausted; refine approved query")


def collect_telegram_export(
    client: InformationClient, raw: bytes, *, channel_id: str
) -> InformationBatch:
    client.authorize()
    if (
        client.source.format != "TELEGRAM_EXPORT"
        or client.source.category != "PUBLIC_RUMOR"
        or client.source.allowed_scope != (channel_id,)
        or re.fullmatch(
            r"https://t\.me/[A-Za-z][A-Za-z0-9_]{4,31}", client.source.endpoint
        )
        is None
        or len(raw) > client.source.max_bytes
    ):
        raise InformationError("approved public channel and bounded export required")
    try:
        body = _OBJECT.validate_python(json.loads(raw, object_pairs_hook=_unique))
        if body.get("type") != "public_channel" or str(body.get("id")) != channel_id:
            raise InformationError("private or different channel export refused")
        rows = TypeAdapter(list[dict[str, Any]]).validate_python(body["messages"])
        if len(rows) > client.source.max_items:
            raise InformationError("export exceeds item budget")
        parsed: list[tuple[str, str, datetime, datetime | None, str]] = []
        for row in rows:
            if row.get("type") != "message" or "rich_message" in row:
                raise InformationError(
                    "unsupported export event; completeness not asserted"
                )
            identifier = str(_integer(row["id"]))
            fragments = row["text"]
            if isinstance(fragments, str):
                text = fragments
            else:
                parts = TypeAdapter(list[str | dict[str, str]]).validate_python(
                    fragments
                )
                text = "".join(
                    part if isinstance(part, str) else part["text"] for part in parts
                )
            if not text.strip():
                raise InformationError(
                    "nontext channel message requires separate handling"
                )
            published = datetime.fromtimestamp(_integer(row["date_unixtime"]), UTC)
            updated = (
                datetime.fromtimestamp(_integer(row["edited_unixtime"]), UTC)
                if "edited_unixtime" in row
                else None
            )
            revision = (
                "sha256:"
                + hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
            )
            parsed.append((identifier, text, published, updated, revision))
        receipt = client.retain(raw, client.clock())
        items = tuple(
            TextObservation(
                source_id=client.source.source_id,
                item_id="message-" + identifier,
                revision=revision,
                observed_at=receipt.observed_at,
                published_at=published,
                updated_at=updated,
                category="PUBLIC_RUMOR",
                title=text[:200],
                text=text,
                url=client.source.endpoint + "/" + identifier,
                correction=updated is not None,
                raw=receipt,
                policy_digest=evidence_digest(client.policy),
            )
            for identifier, text, published, updated, revision in parsed
        )
        return InformationBatch(
            source_digest=evidence_digest(client.source),
            observations=items,
            pages=(receipt,),
            observed_at=client.clock(),
            declared_count=len(items),
            coverage="FEED_WINDOW",
        )
    except (ValueError, KeyError, TypeError, OverflowError):
        raise InformationError(
            "channel export failed source or timestamp validation"
        ) from None
