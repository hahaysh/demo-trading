"""Official KRX reference API adapter, not an eligibility or trading approval."""

import json
from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, SecretStr, TypeAdapter

from ats.data.information import InformationClient, InformationError
from ats.data.storage import StoredPayload
from ats.domain.governance import evidence_digest
from ats.domain.prices import SEOUL
from ats.domain.strategy import FrozenModel, Sha256Digest

ENDPOINT = "https://data-dbg.krx.co.kr/svc/apis/sto/stk_isu_base_info"
KOSDAQ_ENDPOINT = "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_isu_base_info"
KONEX_ENDPOINT = "https://data-dbg.krx.co.kr/svc/apis/sto/knx_isu_base_info"
REFERENCE_SERVICES = {
    ENDPOINT: ("stk_isu_base_info", date(2010, 1, 4)),
    KOSDAQ_ENDPOINT: ("ksq_isu_base_info", date(2010, 1, 4)),
    KONEX_ENDPOINT: ("knx_isu_base_info", date(2013, 7, 1)),
}


class KrxReferenceItem(FrozenModel):
    isin: Annotated[str, Field(pattern="^[A-Z0-9]{12}$")]
    symbol: Annotated[str, Field(pattern="^[A-Z0-9]{6}$")]
    name: str
    abbreviation: str
    listing_date: date
    market: str
    security_group: str
    share_type: str
    listed_shares: Annotated[int, Field(strict=True, ge=0)]


class KrxReferenceBatch(FrozenModel):
    as_of: date
    observed_at: AwareDatetime
    source_digest: Sha256Digest
    policy_digest: Sha256Digest
    raw: StoredPayload
    items: tuple[KrxReferenceItem, ...]
    eligibility_verified: Literal[False] = False
    historical_knowledge_verified: Literal[False] = False


def collect_krx_reference(
    client: InformationClient, *, session: date, auth_key: SecretStr
) -> KrxReferenceBatch:
    service = REFERENCE_SERVICES.get(client.source.endpoint)
    if (
        client.source.format != "KRX_JSON"
        or client.source.category != "MARKET"
        or service is None
        or client.source.allowed_scope != (service[0],)
        or session >= client.clock().astimezone(SEOUL).date()
        or session < service[1]
        or not auth_key.get_secret_value()
    ):
        raise InformationError(
            "fixed approved KRX reference endpoint and completed session required"
        )
    raw, observed = client.fetch(
        {"basDd": session.strftime("%Y%m%d")},
        headers={"AUTH_KEY": auth_key.get_secret_value()},
    )

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate KRX field")
            result[key] = value
        return result

    try:
        body = TypeAdapter(dict[str, list[dict[str, str]]]).validate_python(
            json.loads(raw, object_pairs_hook=unique)
        )
        if (
            set(body) != {"OutBlock_1"}
            or len(body["OutBlock_1"]) > client.source.max_items
        ):
            raise ValueError("KRX response envelope or count mismatch")
        items = tuple(
            KrxReferenceItem.model_validate(
                {
                    "isin": row["ISU_CD"],
                    "symbol": row["ISU_SRT_CD"],
                    "name": row["ISU_NM"],
                    "abbreviation": row["ISU_ABBRV"],
                    "listing_date": date.fromisoformat(row["LIST_DD"]),
                    "market": row["MKT_TP_NM"],
                    "security_group": row["SECUGRP_NM"],
                    "share_type": row["KIND_STKCERT_TP_NM"],
                    "listed_shares": int(row["LIST_SHRS"]),
                }
            )
            for row in body["OutBlock_1"]
        )
        if len({item.isin for item in items}) != len(items) or len(
            {item.symbol for item in items}
        ) != len(items):
            raise ValueError("duplicate reference identities")
        if any(item.listing_date > session for item in items):
            raise ValueError("future listing in historical reference response")
        receipt = client.retain(raw, observed)
        return KrxReferenceBatch(
            as_of=session,
            observed_at=receipt.observed_at,
            source_digest=evidence_digest(client.source),
            policy_digest=evidence_digest(client.policy),
            raw=receipt,
            items=items,
        )
    except (ValueError, KeyError, TypeError):
        raise InformationError("KRX reference response failed validation") from None
