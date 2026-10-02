"""Execute a next-open trend strategy through Qlib's real backtest executor."""

import hashlib
import importlib
import json
import re
import struct
import sys
import tempfile
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any


def deal_price(price: float, data: dict[str, Any], *, buy: bool) -> float:
    value = Decimal(str(price)) * (
        1 + Decimal(str(data["slippage"])) * (1 if buy else -1)
    )
    if data.get("price_tick") is not None:
        tick = Decimal(str(data["price_tick"]))
        value = (value / tick).to_integral_value(
            rounding=ROUND_CEILING if buy else ROUND_FLOOR
        ) * tick
    return float(value)


def run(case: dict[str, Any]) -> dict[str, Any]:
    if case["mode"] != "SYNTHETIC_OFFLINE_ONLY" or not 0 <= case["slippage"] <= 0.05:
        raise ValueError("only bounded synthetic slippage cases are admitted")
    dates = case["dates"]
    shared = "assets" in case
    assets = case.get("assets", [{"instrument_id": "krx-test", "case": case}])
    if not 1 <= len(assets) <= 10 or any(
        not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", asset["instrument_id"])
        for asset in assets
    ):
        raise ValueError("invalid bounded engine instruments")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "calendars").mkdir()
        (root / "instruments").mkdir()
        calendar = "\n".join([*dates, case["next_session"]]) + "\n"
        (root / "calendars" / "day.txt").write_text(calendar)
        (root / "calendars" / "day_future.txt").write_text(calendar)
        (root / "instruments" / "all.txt").write_text(
            "".join(
                f"{asset['instrument_id']}\t{dates[0]}\t{dates[-1]}\n"
                for asset in assets
            )
        )
        for asset in assets:
            data = asset["case"]
            features = root / "features" / asset["instrument_id"]
            features.mkdir(parents=True)
            for field, values in {
                "open": data["open"],
                "buy": [deal_price(price, data, buy=True) for price in data["open"]],
                "sell": [deal_price(price, data, buy=False) for price in data["open"]],
                "close": data["close"],
                "volume": data["volume"],
                "capacity": data.get("opening_capacity")
                or [data["quantity"]] * len(dates),
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
        pending_dividends: dict[str, dict[str, dict[str, Any]]] = {
            asset["instrument_id"]: {} for asset in assets
        }

        class NextOpenStrategy(base):
            def generate_trade_decision(self, execute_result: Any = None) -> Any:
                index = self.trade_calendar.get_trade_step()
                start, end = self.trade_calendar.get_step_time()
                orders = []
                for asset in assets:
                    data, instrument = asset["case"], asset["instrument_id"]
                    actions = data.get("corporate_actions", [])
                    if actions and "targets" not in data:
                        raise ValueError(
                            "corporate actions require explicit raw targets"
                        )
                    position = self.trade_position.position
                    for action in actions:
                        if action["session"] != dates[index]:
                            continue
                        owned = self.trade_position.get_stock_amount(instrument)
                        if action["kind"] == "FORWARD_SPLIT" and owned:
                            position[instrument]["amount"] *= action[
                                "new_shares_per_old"
                            ]
                            position[instrument]["price"] /= action[
                                "new_shares_per_old"
                            ]
                        elif action["kind"] == "NET_CASH_DIVIDEND":
                            pending_dividends[instrument][action["action_id"]] = {
                                "due": action["payment_session"],
                                "amount": owned * action["net_cash_per_share"],
                            }
                    for identifier, claim in tuple(
                        pending_dividends[instrument].items()
                    ):
                        if claim["due"] == dates[index]:
                            position["cash"] += claim["amount"]
                            del pending_dividends[instrument][identifier]
                    history = data["close"][max(0, index - data["lookback"]) : index]
                    long = len(history) == data["lookback"] and history[-1] > sum(
                        history
                    ) / len(history)
                    held = self.trade_position.get_stock_amount(instrument)
                    target = (
                        data["targets"][index]
                        if "targets" in data
                        else data["quantity"]
                        if long
                        else 0
                    )
                    if target != held:
                        orders.append(
                            decisions.Order(
                                stock_id=instrument,
                                amount=abs(target - held),
                                start_time=start,
                                end_time=end,
                                direction=decisions.Order.BUY
                                if target > held
                                else decisions.Order.SELL,
                            )
                        )
                self.trade_position.position["cash_delay"] = sum(
                    claim["amount"]
                    for claims in pending_dividends.values()
                    for claim in claims.values()
                )
                orders.sort(
                    key=lambda order: (
                        order.direction == decisions.Order.BUY,
                        order.stock_id,
                    )
                )
                return decisions.TradeDecisionWO(orders, self)

            def post_exe_step(self, execute_result: Any) -> None:
                for order, _value, cost, price in execute_result:
                    if order.deal_amount:
                        fills.append(
                            {
                                **({"instrument_id": order.stock_id} if shared else {}),
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
                "codes": [asset["instrument_id"] for asset in assets],
                "deal_price": ("$buy", "$sell"),
                "trade_unit": 1,
                "volume_threshold": ("current", "$capacity"),
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
