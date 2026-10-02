"""Execute a next-open trend strategy through Qlib's real backtest executor."""

import hashlib
import importlib
import json
import struct
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import Any


def run(case: dict[str, Any]) -> dict[str, Any]:
    if case["mode"] != "SYNTHETIC_OFFLINE_ONLY" or case["slippage"] != 0:
        raise ValueError("only explicit zero-slippage synthetic cases are admitted")
    dates = case["dates"]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "calendars").mkdir()
        (root / "instruments").mkdir()
        features = root / "features" / "krx-test"
        features.mkdir(parents=True)
        calendar = "\n".join([*dates, case["next_session"]]) + "\n"
        (root / "calendars" / "day.txt").write_text(calendar)
        (root / "calendars" / "day_future.txt").write_text(calendar)
        (root / "instruments" / "all.txt").write_text(
            f"krx-test\t{dates[0]}\t{dates[-1]}\n"
        )
        for field, values in {
            "open": case["open"],
            "close": case["close"],
            "volume": case["volume"],
            "factor": [1] * len(dates),
            "change": [0] * len(dates),
        }.items():
            (features / f"{field}.day.bin").write_bytes(
                struct.pack(f"<{len(values) + 1}f", 0, *values)
            )
        qlib = importlib.import_module("qlib")
        qlib.init(
            provider_uri=str(root),
            expression_cache=None,
            dataset_cache=None,
            kernels=1,
            joblib_backend="threading",
            trade_unit=1,
            limit_threshold=None,
        )
        base = importlib.import_module("qlib.strategy.base").BaseStrategy
        decisions = importlib.import_module("qlib.backtest.decision")
        fills: list[dict[str, Any]] = []

        class NextOpenStrategy(base):
            def generate_trade_decision(self, execute_result: Any = None) -> Any:
                index = self.trade_calendar.get_trade_step()
                start, end = self.trade_calendar.get_step_time()
                history = case["close"][max(0, index - case["lookback"]) : index]
                long = len(history) == case["lookback"] and history[-1] > sum(
                    history
                ) / len(history)
                held = self.trade_position.get_stock_amount("krx-test")
                target = (
                    case["targets"][index]
                    if "targets" in case
                    else case["quantity"]
                    if long
                    else 0
                )
                orders = []
                if target != held:
                    orders.append(
                        decisions.Order(
                            stock_id="krx-test",
                            amount=abs(target - held),
                            start_time=start,
                            end_time=end,
                            direction=decisions.Order.BUY
                            if target > held
                            else decisions.Order.SELL,
                        )
                    )
                return decisions.TradeDecisionWO(orders, self)

            def post_exe_step(self, execute_result: Any) -> None:
                for order, _value, cost, price in execute_result:
                    if order.deal_amount:
                        fills.append(
                            {
                                "date": str(order.start_time.date()),
                                "quantity": float(order.deal_amount),
                                "side": "BUY"
                                if order.direction == decisions.Order.BUY
                                else "SELL",
                                "price": float(price),
                                "cost": float(cost),
                            }
                        )

        backtest = importlib.import_module("qlib.backtest").backtest
        pandas = importlib.import_module("pandas")
        portfolio, _ = backtest(
            start_time=dates[0],
            end_time=dates[-1],
            strategy=NextOpenStrategy(),
            executor={
                "class": "SimulatorExecutor",
                "module_path": "qlib.backtest.executor",
                "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True},
            },
            benchmark=pandas.Series(0.0, index=pandas.to_datetime(dates)),
            account=case["initial_cash"],
            exchange_kwargs={
                "codes": ["krx-test"],
                "deal_price": "$open",
                "trade_unit": 1,
                "limit_threshold": None,
                "open_cost": case["buy_fee"],
                "close_cost": case["sell_fee"],
                "min_cost": 0,
            },
        )
        report, _ = portfolio["1day"]
        return {
            "engine": "QLIB",
            "version": version("pyqlib"),
            "full_backtest": True,
            "certified": False,
            "fills": fills,
            "equity": [float(value) for value in report["account"].tolist()],
        }


if __name__ == "__main__":
    payload = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError("input exceeds byte budget")
    result = run(json.loads(payload))
    result["input_sha256"] = hashlib.sha256(payload).hexdigest()
    result["code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["lock_sha256"] = hashlib.sha256(
        Path("/opt/requirements.lock").read_bytes()
    ).hexdigest()
    print(json.dumps(result, sort_keys=True, allow_nan=False))
