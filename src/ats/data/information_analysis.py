"""Auditable point-in-time text features; not a fact oracle or order generator."""

import hashlib
import re
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from typing import Annotated, Literal, Self, cast

from pydantic import AwareDatetime, Field, model_validator

from ats.data.information import TextObservation
from ats.domain.governance import evidence_digest
from ats.domain.strategy import ArtifactRef, FrozenModel, Identifier, Sha256Digest


class EntityAlias(FrozenModel):
    instrument_id: Identifier
    company_id: str
    symbol: Annotated[str, Field(pattern="^[A-Z0-9]{6}$")]
    aliases: tuple[str, ...]
    known_at: AwareDatetime
    effective_from: AwareDatetime
    effective_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if (
            self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("entity alias interval must be nonempty")
        if not self.company_id.strip() or any(
            not name.strip() for name in self.aliases
        ):
            raise ValueError("entity identifiers and aliases cannot be blank")
        return self


class SourceAssessment(FrozenModel):
    source_id: Identifier
    publisher_group: Identifier
    tier: Literal["PRIMARY", "LICENSED", "PUBLIC", "LOW_TRUST"]


class AnalysisPolicy(FrozenModel):
    artifact: ArtifactRef
    sources: tuple[SourceAssessment, ...]
    aliases: tuple[EntityAlias, ...]
    rumor_cap: Annotated[float, Field(ge=0, le=0.1)] = 0.1
    max_items: Annotated[int, Field(strict=True, ge=1, le=1000)] = 500
    agent_editable: Literal[False] = False

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("analysis sources must be unique")
        return self


class AnalyzedText(FrozenModel):
    observation_digest: Sha256Digest
    source_id: Identifier
    item_id: Identifier
    revision: Sha256Digest
    raw_digest: Sha256Digest
    available_at: AwareDatetime
    instruments: tuple[Identifier, ...]
    event: Literal["EARNINGS", "CONTRACT", "CAPITAL", "MERGER", "GOVERNANCE", "OTHER"]
    stance: Literal["REPORT", "DENIAL", "RETRACTION", "CORRECTION"]
    rumor: bool
    publisher_group: Identifier
    tier: Literal["PRIMARY", "LICENSED", "PUBLIC", "LOW_TRUST"]
    content_cluster: Sha256Digest
    related_disclosures: tuple[Sha256Digest, ...] = ()
    counterevidence: tuple[Sha256Digest, ...] = ()
    superseded: bool = False
    instruction_like_text: bool
    uncertainty: Literal["UNVERIFIED", "RELATED_EVIDENCE_ONLY"] = "UNVERIFIED"


class TextFeatures(FrozenModel):
    instrument_id: Identifier
    cutoff: AwareDatetime
    official_events: Annotated[int, Field(strict=True, ge=0)]
    independent_groups: Annotated[int, Field(strict=True, ge=0)]
    rumor_fraction: Annotated[float, Field(ge=0, le=0.1)]
    conflicting_evidence: bool
    evidence: tuple[Sha256Digest, ...]
    order_authorized: Literal[False] = False


class InformationAnalysis(FrozenModel):
    analyzer: Literal["auditable-rules-v1"] = "auditable-rules-v1"
    model_claim: Literal["RULES_NOT_LEARNED_AI"] = "RULES_NOT_LEARNED_AI"
    policy_digest: Sha256Digest
    cutoff: AwareDatetime
    items: tuple[AnalyzedText, ...]
    features: tuple[TextFeatures, ...]


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "iframe", "object"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "iframe", "object"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(text: str) -> str:
    parser = _PlainText()
    parser.feed(text)
    parser.close()
    return " ".join(unicodedata.normalize("NFKC", " ".join(parser.parts)).split())


def analyze_information(
    observations: tuple[TextObservation, ...], policy: AnalysisPolicy, *, at: datetime
) -> InformationAnalysis:
    policy = AnalysisPolicy.model_validate(policy.model_dump())
    if (
        at.tzinfo is None
        or at.utcoffset() is None
        or len(observations) > policy.max_items
    ):
        raise ValueError("aware analysis cutoff and bounded observations required")
    assessments = {source.source_id: source for source in policy.sources}
    latest: dict[tuple[str, str], TextObservation] = {}
    superseded: set[tuple[str, str]] = set()
    for supplied in observations:
        item = TextObservation.model_validate(supplied.model_dump())
        if item.observed_at > at:
            continue
        if item.raw.expires_at <= at or item.source_id not in assessments:
            raise ValueError(
                "analysis cannot use expired or unapproved source evidence"
            )
        key = (item.source_id, item.item_id)
        previous = latest.get(key)
        if (
            previous is not None
            and previous.observed_at == item.observed_at
            and previous.revision != item.revision
        ):
            raise ValueError("same-time text revisions are ambiguous")
        if previous is None or previous.observed_at < item.observed_at:
            latest[key] = item
    for item in latest.values():
        if item.supersedes:
            previous = latest.get((item.source_id, item.supersedes))
            if (
                not (item.correction or item.retraction)
                or previous is None
                or previous.item_id == item.item_id
                or previous.observed_at > item.observed_at
                or item.company_id is not None
                and previous.company_id != item.company_id
            ):
                raise ValueError("correction requires an earlier same-company original")
            visited = {item.item_id}
            ancestor = previous
            while True:
                if ancestor.item_id in visited:
                    raise ValueError("cyclic correction links are forbidden")
                visited.add(ancestor.item_id)
                if not ancestor.supersedes:
                    break
                target = latest.get((ancestor.source_id, ancestor.supersedes))
                if target is None:
                    raise ValueError("correction chain is incomplete")
                ancestor = target
            superseded.add((item.source_id, item.supersedes))
    analyzed: list[AnalyzedText] = []
    for key in sorted(latest):
        item = latest[key]
        text = plain_text(item.title + " " + item.text)
        body = plain_text(item.text)
        aliases = [
            alias
            for alias in policy.aliases
            if alias.known_at <= item.observed_at
            and alias.effective_from <= item.observed_at
            and (
                alias.effective_until is None
                or item.observed_at < alias.effective_until
            )
        ]
        strong = {
            alias.instrument_id
            for alias in aliases
            if item.company_id is not None
            and alias.company_id == item.company_id
            or item.symbol is not None
            and alias.symbol == item.symbol
        }
        matched = strong or {
            alias.instrument_id
            for alias in aliases
            if any(name and name in text for name in alias.aliases)
        }
        if len(strong) > 1:
            raise ValueError("company identifier and symbol conflict")
        event: Literal[
            "EARNINGS", "CONTRACT", "CAPITAL", "MERGER", "GOVERNANCE", "OTHER"
        ] = "OTHER"
        for pattern, candidate in (
            (r"실적|매출|earnings|revenue", "EARNINGS"),
            (r"계약|수주|contract", "CONTRACT"),
            (r"유상증자|감자|배당|capital|dividend", "CAPITAL"),
            (r"합병|인수|merger|acquisition", "MERGER"),
            (r"대표이사|횡령|governance", "GOVERNANCE"),
        ):
            if re.search(pattern, text, re.IGNORECASE):
                event = cast(
                    Literal[
                        "EARNINGS",
                        "CONTRACT",
                        "CAPITAL",
                        "MERGER",
                        "GOVERNANCE",
                        "OTHER",
                    ],
                    candidate,
                )
                break
        stance: Literal["REPORT", "DENIAL", "RETRACTION", "CORRECTION"] = "REPORT"
        if item.retraction or re.search(r"철회|retract", text, re.IGNORECASE):
            stance = "RETRACTION"
        elif re.search(r"사실무근|부인|denied|not true", text, re.IGNORECASE):
            stance = "DENIAL"
        elif item.correction:
            stance = "CORRECTION"
        source = assessments[item.source_id]
        analyzed.append(
            AnalyzedText(
                observation_digest=evidence_digest(item),
                source_id=item.source_id,
                item_id=item.item_id,
                revision=item.revision,
                raw_digest=item.raw.digest,
                available_at=item.observed_at,
                instruments=tuple(sorted(matched)),
                event=event,
                stance=stance,
                rumor=item.category == "PUBLIC_RUMOR",
                publisher_group=source.publisher_group,
                tier=source.tier,
                content_cluster="sha256:"
                + hashlib.sha256(body.casefold().encode()).hexdigest(),
                superseded=key in superseded,
                instruction_like_text=bool(
                    re.search(
                        r"ignore.*instructions|system\s*prompt|지시.*무시|키.*전송",
                        text,
                        re.IGNORECASE,
                    )
                ),
            )
        )
    enriched: list[AnalyzedText] = []
    for item in analyzed:
        related = tuple(
            other.observation_digest
            for other in analyzed
            if other != item
            and not other.superseded
            and other.tier == "PRIMARY"
            and not other.rumor
            and other.event == item.event
            and item.event != "OTHER"
            and set(other.instruments) & set(item.instruments)
            and other.content_cluster != item.content_cluster
        )
        counters = tuple(
            other.observation_digest
            for other in analyzed
            if other.observation_digest in related
            and other.stance in ("DENIAL", "RETRACTION")
        )
        enriched.append(
            AnalyzedText.model_validate(
                {
                    **item.model_dump(),
                    "related_disclosures": related,
                    "counterevidence": counters,
                    "uncertainty": "RELATED_EVIDENCE_ONLY" if related else "UNVERIFIED",
                }
            )
        )
    features: list[TextFeatures] = []
    for instrument in sorted(
        {instrument for item in enriched for instrument in item.instruments}
    ):
        items = [
            item
            for item in enriched
            if instrument in item.instruments and not item.superseded
        ]
        clusters: dict[str, set[str]] = {}
        for item in items:
            clusters.setdefault(item.content_cluster, set()).add(item.publisher_group)
        independent = len({min(groups) for groups in clusters.values()})
        official = len(
            {
                item.content_cluster
                for item in items
                if item.tier == "PRIMARY"
                and not item.rumor
                and item.stance in ("REPORT", "CORRECTION")
            }
        )
        conflict = any(
            item.counterevidence or item.stance in ("DENIAL", "RETRACTION")
            for item in items
        )
        rumor = (
            policy.rumor_cap
            if official and not conflict and any(item.rumor for item in items)
            else 0.0
        )
        features.append(
            TextFeatures(
                instrument_id=instrument,
                cutoff=at,
                official_events=official,
                independent_groups=independent,
                rumor_fraction=rumor,
                conflicting_evidence=conflict,
                evidence=tuple(item.observation_digest for item in items),
            )
        )
    return InformationAnalysis(
        policy_digest=evidence_digest(policy),
        cutoff=at,
        items=tuple(enriched),
        features=tuple(features),
    )
