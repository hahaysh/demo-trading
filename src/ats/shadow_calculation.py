"""Pinned Python candidate execution with no network, mounts, or inherited secrets."""

import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter

from ats.domain.governance import evidence_digest
from ats.domain.strategy import StrategySpec
from ats.kis_readonly import ProductionAccountObservation
from ats.shadow import ShadowProposal


def calculate_proposals(
    candidate: StrategySpec,
    program: bytes,
    observation: ProductionAccountObservation,
    *,
    at: datetime,
    timeout_seconds: float = 60,
) -> tuple[ShadowProposal, ...]:
    candidate = StrategySpec.model_validate(candidate.model_dump())
    observation = ProductionAccountObservation.model_validate(observation.model_dump())
    if (
        not 0 < timeout_seconds <= 60
        or len(program) > 65536
        or "sha256:" + hashlib.sha256(program).hexdigest()
        != candidate.code_artifact.digest
    ):
        raise ValueError("candidate executable hash or budget mismatch")
    payload = json.dumps(
        {
            "protocol": "ats.shadow.v1",
            "candidate": candidate.model_dump(mode="json"),
            "candidate_digest": candidate.content_digest(),
            "observation": observation.model_dump(mode="json"),
            "observation_digest": evidence_digest(observation),
            "at": at.isoformat(),
        },
        sort_keys=True,
        allow_nan=False,
    ).encode()
    if len(payload) > 1048576:
        raise ValueError("shadow input exceeds budget")
    docker = [
        "docker",
        "--host",
        "npipe:////./pipe/dockerDesktopLinuxEngine"
        if os.name == "nt"
        else "unix:///var/run/docker.sock",
    ]
    name = "ats-shadow-" + uuid4().hex
    command = [
        *docker,
        "run",
        "--rm",
        "--pull",
        "never",
        "-i",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--user",
        "65532:65532",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "32",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=16m",
        "--log-driver",
        "none",
        "--entrypoint",
        "timeout",
        candidate.container_image_digest,
        str(timeout_seconds) + "s",
        "python",
        "-I",
        "-B",
        "-c",
        program.decode("utf-8"),
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP"}
    }
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    output = process.stdout
    input_stream = process.stdin

    def send_input() -> None:
        try:
            input_stream.write(payload)
        finally:
            input_stream.close()

    pool = ThreadPoolExecutor(max_workers=2)
    future = pool.submit(output.read, 1048577)
    sent = pool.submit(send_input)
    try:
        raw = future.result(timeout=timeout_seconds)
        sent.result(timeout=1)
        if len(raw) > 1048576 or process.wait(timeout=5) != 0:
            raise ValueError("shadow worker rejected or exceeded output budget")
        result = TypeAdapter(dict[str, Any]).validate_json(raw)
        if (
            result.get("protocol") != "ats.shadow.v1"
            or result.get("input_digest")
            != "sha256:" + hashlib.sha256(payload).hexdigest()
        ):
            raise ValueError("shadow worker response binding mismatch")
        proposals = TypeAdapter(tuple[ShadowProposal, ...]).validate_python(
            result.get("proposals")
        )
        if not 1 <= len(proposals) <= 100 or any(
            item.candidate_digest != candidate.content_digest()
            or item.observation_digest != evidence_digest(observation)
            or item.account_id != observation.account_id
            or item.created_at != at
            for item in proposals
        ):
            raise ValueError("shadow worker proposals do not match pinned inputs")
        return proposals
    except Exception:
        raise ValueError(
            "isolated shadow calculation failed; no worker logs retained"
        ) from None
    finally:
        try:
            subprocess.run(
                [*docker, "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                env=environment,
                check=False,
            )
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            pool.shutdown(wait=True, cancel_futures=True)
