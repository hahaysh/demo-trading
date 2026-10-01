"""Fixed synthetic Qlib feature probe, not an evaluation or promotion artifact."""

import hashlib
import importlib
import json
import struct
import tempfile
from importlib.metadata import version
from pathlib import Path


def main() -> None:
    closes = (100.0, 110.0, 90.0, 80.0, 100.0)
    dates = tuple(f"2026-09-{day}" for day in range(21, 26))
    with tempfile.TemporaryDirectory(prefix="ats-qlib-") as directory:
        root = Path(directory)
        (root / "calendars").mkdir()
        (root / "instruments").mkdir()
        features = root / "features" / "krx-test"
        features.mkdir(parents=True)
        (root / "calendars" / "day.txt").write_text("\n".join(dates) + "\n")
        (root / "instruments" / "all.txt").write_text(
            f"krx-test\t{dates[0]}\t{dates[-1]}\n"
        )
        (features / "close.day.bin").write_bytes(struct.pack("<6f", 0, *closes))
        qlib = importlib.import_module("qlib")
        qlib.init(
            provider_uri=str(root),
            expression_cache=None,
            dataset_cache=None,
            kernels=1,
            joblib_backend="threading",
        )
        data = importlib.import_module("qlib.data")
        frame = data.D.features(
            ["krx-test"],
            ["$close", "Mean($close, 2)"],
            start_time=dates[0],
            end_time=dates[-1],
            freq="day",
        )
        observed = [[float(value) for value in row] for row in frame.to_numpy()]
        expected = [
            [100.0, 100.0],
            [110.0, 105.0],
            [90.0, 100.0],
            [80.0, 85.0],
            [100.0, 90.0],
        ]
        if observed != expected:
            raise ValueError("Qlib synthetic rolling feature differs from reference")
        print(
            json.dumps(
                {
                    "kind": "QlibSyntheticFeatureProbe",
                    "engine_version": version("pyqlib"),
                    "dependency_lock_sha256": hashlib.sha256(
                        Path("/opt/requirements.lock").read_bytes()
                    ).hexdigest(),
                    "mode": "SYNTHETIC_OFFLINE_ONLY",
                    "certified": False,
                    "rows": observed,
                    "signals": [close > mean for close, mean in observed],
                    "broker_requests_sent": 0,
                    "evaluation_result_created": False,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
