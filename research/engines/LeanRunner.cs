using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using QuantConnect;
using QuantConnect.Algorithm;
using QuantConnect.Configuration;
using QuantConnect.Data;
using QuantConnect.Lean.Engine;
using QuantConnect.Orders;
using QuantConnect.Orders.Fees;
using QuantConnect.Securities;
using QuantConnect.Util;

public static class Entry
{
    public static decimal ExecutionPrice(decimal price, bool buy, decimal slippage, decimal? tick)
    {
        var value = price * (buy ? 1m + slippage : 1m - slippage);
        if (tick is not null) value = (buy ? Math.Ceiling(value / tick.Value) : Math.Floor(value / tick.Value)) * tick.Value;
        return value;
    }

    public static IEnumerable<(string Name, JsonElement Data)> Assets(JsonElement settings)
    {
        if (settings.TryGetProperty("assets", out var assets))
        {
            if (assets.GetArrayLength() is < 2 or > 10) throw new ArgumentException("Invalid portfolio size");
            foreach (var asset in assets.EnumerateArray())
            {
                var name = asset.GetProperty("instrument_id").GetString()!;
                if (!System.Text.RegularExpressions.Regex.IsMatch(name, "^[a-z][a-z0-9-]{2,63}$")) throw new ArgumentException("Invalid instrument");
                yield return (name, asset.GetProperty("case"));
            }
        }
        else yield return ("krx-test", settings);
    }

    public static int Main()
    {
        var payload = Console.In.ReadToEnd();
        if (payload.Length > 1024 * 1024) throw new ArgumentException("Input too large");
        var data = JsonDocument.Parse(payload).RootElement;
        if (data.GetProperty("mode").GetString() != "SYNTHETIC_OFFLINE_ONLY" ||
            data.GetProperty("slippage").GetDecimal() is < 0 or > 0.05m) throw new ArgumentException("Unsupported mode");
        Directory.CreateDirectory("/tmp/ats/data/market-hours");
        Directory.CreateDirectory("/tmp/ats/data/symbol-properties");
        Directory.SetCurrentDirectory("/tmp/ats");
        File.WriteAllText("case.json", payload);
        var hours = new Dictionary<string, object> {
            ["dataTimeZone"] = "Asia/Seoul", ["exchangeTimeZone"] = "Asia/Seoul",
            ["holidays"] = Array.Empty<string>()
        };
        foreach (var day in Enum.GetNames<DayOfWeek>())
            hours[day.ToLowerInvariant()] = new[] { new { start = "00:00:00", end = "1.00:00:00", state = "market" } };
        File.WriteAllText("data/market-hours/market-hours-database.json", JsonSerializer.Serialize(new {
            entries = new Dictionary<string, object> { ["Base-usa-[*]"] = hours }
        }));
        File.WriteAllText("data/symbol-properties/symbol-properties-database.csv",
            "#market,symbol,type,description,quote_currency,contract_multiplier,minimum_price_variation,lot_size,market_ticker,minimum_order_size,price_magnifier,strike_multiplier\n" +
            "usa,*,base,Synthetic,KRW,1,0.01,1,KRXTEST,,1,1\n");
        var dates = System.Linq.Enumerable.Select(data.GetProperty("dates").EnumerateArray(), value => value.GetString()!).ToArray();
        foreach (var asset in Assets(data))
        {
            Directory.CreateDirectory($"data/{asset.Name}");
            for (var index = 0; index < dates.Length; index++)
                File.WriteAllText($"data/{asset.Name}/{dates[index]}.csv",
                    $"{dates[index]}T09:00:00,{asset.Data.GetProperty("open")[index].GetDecimal().ToString(CultureInfo.InvariantCulture)}\n" +
                    $"{dates[index]}T15:30:00,{asset.Data.GetProperty("close")[index].GetDecimal().ToString(CultureInfo.InvariantCulture)}\n");
        }
        var settings = new Dictionary<string, string> {
            ["environment"] = "backtesting", ["live-mode"] = "false",
            ["algorithm-type-name"] = "SyntheticNextOpen", ["algorithm-language"] = "CSharp",
            ["algorithm-location"] = "/opt/runner/Runner.dll", ["data-folder"] = "/tmp/ats/data",
            ["results-destination-folder"] = "/tmp/ats/results", ["object-store-root"] = "/tmp/ats/storage",
            ["log-handler"] = "QuantConnect.Logging.ConsoleLogHandler",
            ["messaging-handler"] = "QuantConnect.Messaging.Messaging",
            ["job-queue-handler"] = "QuantConnect.Queues.JobQueue", ["api-handler"] = "QuantConnect.Api.Api",
            ["setup-handler"] = "QuantConnect.Lean.Engine.Setup.BacktestingSetupHandler",
            ["result-handler"] = "OfflineResultHandler",
            ["data-feed-handler"] = "QuantConnect.Lean.Engine.DataFeeds.FileSystemDataFeed",
            ["real-time-handler"] = "QuantConnect.Lean.Engine.RealTime.BacktestingRealTimeHandler",
            ["history-provider"] = "QuantConnect.Lean.Engine.HistoricalData.SubscriptionDataReaderHistoryProvider",
            ["transaction-handler"] = "QuantConnect.Lean.Engine.TransactionHandlers.BacktestingTransactionHandler",
            ["data-provider"] = "QuantConnect.Lean.Engine.DataFeeds.DefaultDataProvider",
            ["map-file-provider"] = "QuantConnect.Data.Auxiliary.LocalDiskMapFileProvider",
            ["factor-file-provider"] = "QuantConnect.Data.Auxiliary.LocalDiskFactorFileProvider",
            ["object-store"] = "QuantConnect.Lean.Engine.Storage.LocalObjectStore",
            ["composer-dll-directory"] = "/opt/runner", ["job-user-id"] = "0",
            ["job-organization-id"] = "", ["api-access-token"] = "",
            ["force-exchange-always-open"] = "true", ["show-missing-data-logs"] = "true"
        };
        foreach (var setting in settings) Config.Set(setting.Key, setting.Value);
        Initializer.Start();
        using var system = Initializer.GetSystemHandlers();
        var job = system.JobQueue.NextJob(out var assemblyPath);
        using var handlers = Initializer.GetAlgorithmHandlers();
        var manager = new AlgorithmManager(false, job);
        system.LeanManager.Initialize(system, handlers, job, manager);
        OS.Initialize();
        new QuantConnect.Lean.Engine.Engine(system, handlers, false).Run(job, manager, assemblyPath, WorkerThread.Instance);
        Console.SetOut(new StreamWriter(Console.OpenStandardOutput()) { AutoFlush = true });
        Console.SetError(new StreamWriter(Console.OpenStandardError()) { AutoFlush = true });
        if (manager.State != AlgorithmStatus.Completed || !File.Exists("/tmp/ats/result.json")) return 1;
        var result = JsonSerializer.Deserialize<Dictionary<string, object>>(File.ReadAllText("/tmp/ats/result.json"))!;
        result["input_sha256"] = Convert.ToHexString(SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(payload))).ToLowerInvariant();
        result["lock_sha256"] = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes("/opt/runner/packages.lock.json"))).ToLowerInvariant();
        result["code_sha256"] = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes("/opt/runner/LeanRunner.cs"))).ToLowerInvariant();
        Console.WriteLine(JsonSerializer.Serialize(result));
        return 0;
    }
}

public class SyntheticPoint : BaseData
{
    public override SubscriptionDataSource GetSource(SubscriptionDataConfig config, DateTime date, bool isLiveMode) =>
        new($"/tmp/ats/data/{config.Symbol.Value.ToLowerInvariant()}/{date:yyyy-MM-dd}.csv", SubscriptionTransportMedium.LocalFile);

    public override BaseData Reader(SubscriptionDataConfig config, string line, DateTime date, bool isLiveMode)
    {
        var fields = line.Split(',');
        return new SyntheticPoint { Symbol = config.Symbol,
            Time = DateTime.Parse(fields[0], CultureInfo.InvariantCulture),
            Value = decimal.Parse(fields[1], CultureInfo.InvariantCulture) };
    }
}

public class SyntheticNextOpen : QCAlgorithm
{
    private readonly Dictionary<Symbol, (string Name, JsonElement Data)> instruments = [];
    private JsonElement settings;
    private readonly Dictionary<Symbol, List<decimal>> histories = [];
    private readonly List<decimal> equity = [];
    private readonly List<object> fills = [];
    private int sessionIndex;
    private readonly Dictionary<(Symbol Symbol, string Id), (DateTime Due, decimal Amount)> dividendClaims = [];

    private void ApplyCorporateActions(Symbol symbol, JsonElement data)
    {
        if (!data.TryGetProperty("corporate_actions", out var actions)) return;
        if (actions.GetArrayLength() > 0 && !data.TryGetProperty("targets", out _)) throw new ArgumentException("Corporate actions require explicit raw targets");
        foreach (var action in actions.EnumerateArray())
        {
            if (DateTime.Parse(action.GetProperty("session").GetString()!, CultureInfo.InvariantCulture).Date != Time.Date) continue;
            var holding = Portfolio[symbol];
            if (action.GetProperty("kind").GetString() == "FORWARD_SPLIT")
            {
                var ratio = action.GetProperty("new_shares_per_old").GetDecimal();
                holding.SetHoldings(holding.AveragePrice / ratio, holding.Quantity * ratio);
                Portfolio.InvalidateTotalPortfolioValue();
            }
            else
            {
                var amount = holding.Quantity * action.GetProperty("net_cash_per_share").GetDecimal();
                var due = DateTime.Parse(action.GetProperty("payment_session").GetString()!, CultureInfo.InvariantCulture).Date;
                dividendClaims.Add((symbol, action.GetProperty("action_id").GetString()!), (due, amount));
                Portfolio.UnsettledCashBook["KRW"].AddAmount(amount);
            }
        }
        foreach (var claim in dividendClaims.Where(item => item.Key.Symbol == symbol && item.Value.Due == Time.Date).ToArray())
        {
            Portfolio.UnsettledCashBook["KRW"].AddAmount(-claim.Value.Amount);
            Portfolio.CashBook["KRW"].AddAmount(claim.Value.Amount);
            Portfolio[symbol].AddNewDividend(claim.Value.Amount);
            dividendClaims.Remove(claim.Key);
        }
    }

    public override void Initialize()
    {
        settings = JsonDocument.Parse(File.ReadAllText("/tmp/ats/case.json")).RootElement;
        var dates = System.Linq.Enumerable.Select(settings.GetProperty("dates").EnumerateArray(), value => DateTime.Parse(value.GetString()!, CultureInfo.InvariantCulture)).ToArray();
        SetStartDate(dates[0]); SetEndDate(dates[^1]); SetTimeZone("Asia/Seoul");
        SetAccountCurrency("KRW"); SetCash(settings.GetProperty("initial_cash").GetDecimal());
        SetBrokerageModel(QuantConnect.Brokerages.BrokerageName.Default, AccountType.Cash);
        SetRiskFreeInterestRateModel(new ConstantRiskFreeRateInterestRateModel(0m));
        foreach (var asset in Entry.Assets(settings))
        {
            var security = AddData<SyntheticPoint>(asset.Name, Resolution.Minute, NodaTime.DateTimeZoneProviders.Tzdb["Asia/Seoul"], fillForward: false, leverage: 1m);
            instruments.Add(security.Symbol, asset);
            histories.Add(security.Symbol, []);
            var slippage = asset.Data.GetProperty("slippage").GetDecimal();
            var tick = asset.Data.TryGetProperty("price_tick", out var tickValue) && tickValue.ValueKind == JsonValueKind.Number ? tickValue.GetDecimal() : (decimal?)null;
            security.SetSlippageModel(tick is null ? new QuantConnect.Orders.Slippage.ConstantSlippageModel(slippage) : new SyntheticTickSlippage(slippage, tick.Value));
            security.SetFeeModel(new SyntheticFees(asset.Data.GetProperty("buy_fee").GetDecimal(), asset.Data.GetProperty("sell_fee").GetDecimal(), slippage, tick));
        }
        SetBenchmark(time => 1m);
    }

    public override void OnData(Slice slice)
    {
        if (instruments.Keys.Any(symbol => !slice.ContainsKey(symbol))) return;
        if (Time.TimeOfDay == TimeSpan.FromHours(9))
        {
            var orders = new List<(Symbol Symbol, string Name, decimal Delta)>();
            foreach (var instrument in instruments)
            {
                var data = instrument.Value.Data;
                ApplyCorporateActions(instrument.Key, data);
                var history = histories[instrument.Key];
                var lookback = data.GetProperty("lookback").GetInt32();
                var target = history.Count >= lookback && history[^1] > history.TakeLast(lookback).Average()
                    ? data.GetProperty("quantity").GetDecimal() : 0m;
                if (data.TryGetProperty("targets", out var targets)) target = targets[sessionIndex].GetDecimal();
                var delta = target - Portfolio[instrument.Key].Quantity;
                if (data.TryGetProperty("opening_capacity", out var capacity) && capacity.ValueKind == JsonValueKind.Array)
                    delta = Math.Sign(delta) * Math.Min(Math.Abs(delta), capacity[sessionIndex].GetDecimal());
                if (delta != 0) orders.Add((instrument.Key, instrument.Value.Name, delta));
            }
            foreach (var order in orders.OrderBy(order => order.Delta > 0).ThenBy(order => order.Name, StringComparer.Ordinal))
                MarketOrder(order.Symbol, order.Delta);
        }
        else if (Time.TimeOfDay == TimeSpan.FromHours(15.5))
        {
            foreach (var instrument in instruments.Keys) histories[instrument].Add(slice.Get<SyntheticPoint>(instrument).Value);
            equity.Add(Portfolio.TotalPortfolioValue);
            sessionIndex++;
        }
    }

    public override void OnOrderEvent(OrderEvent update)
    {
        if (update.Status == OrderStatus.Filled)
        {
            var fill = new Dictionary<string, object> {
                ["date"] = Time.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
                ["side"] = update.FillQuantity > 0 ? "BUY" : "SELL", ["quantity"] = Math.Abs(update.FillQuantity),
                ["price"] = update.FillPrice, ["cost"] = update.OrderFee.Value.Amount
            };
            if (settings.TryGetProperty("assets", out _)) fill["instrument_id"] = instruments[update.Symbol].Name;
            fills.Add(fill);
        }
    }

    public override void OnEndOfAlgorithm() => File.WriteAllText("/tmp/ats/result.json", JsonSerializer.Serialize(new {
        engine = "LEAN", version = "2.5.18127", full_backtest = true, certified = false, fills, equity
    }));
}

public class SyntheticTickSlippage(decimal slippage, decimal tick) : QuantConnect.Orders.Slippage.ISlippageModel
{
    public decimal GetSlippageApproximation(Security asset, Order order) =>
        Math.Abs(Entry.ExecutionPrice(asset.Price, order.Direction == OrderDirection.Buy, slippage, tick) - asset.Price);
}

public class SyntheticFees(decimal buy, decimal sell, decimal slippage, decimal? tick) : FeeModel
{
    public override OrderFee GetOrderFee(OrderFeeParameters parameters) => new(new CashAmount(
        Math.Abs(parameters.Order.Quantity) * Entry.ExecutionPrice(parameters.Security.Price, parameters.Order.Direction == OrderDirection.Buy, slippage, tick) *
        (parameters.Order.Direction == OrderDirection.Buy ? buy : sell), "KRW"));
}

public class OfflineResultHandler : QuantConnect.Lean.Engine.Results.BacktestingResultHandler
{
    public OfflineResultHandler() { RunResultsAnalysis = false; }
}