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
    public static int Main()
    {
        var payload = Console.In.ReadToEnd();
        if (payload.Length > 1024 * 1024) throw new ArgumentException("Input too large");
        var data = JsonDocument.Parse(payload).RootElement;
        if (data.GetProperty("mode").GetString() != "SYNTHETIC_OFFLINE_ONLY" ||
            data.GetProperty("slippage").GetDecimal() != 0) throw new ArgumentException("Unsupported mode");
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
        for (var index = 0; index < dates.Length; index++)
            File.WriteAllText($"data/{dates[index]}.csv",
                $"{dates[index]}T09:00:00,{data.GetProperty("open")[index].GetDecimal().ToString(CultureInfo.InvariantCulture)}\n" +
                $"{dates[index]}T15:30:00,{data.GetProperty("close")[index].GetDecimal().ToString(CultureInfo.InvariantCulture)}\n");
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
        new($"/tmp/ats/data/{date:yyyy-MM-dd}.csv", SubscriptionTransportMedium.LocalFile);

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
    private Symbol instrument = null!;
    private JsonElement settings;
    private readonly List<decimal> history = [];
    private readonly List<decimal> equity = [];
    private readonly List<object> fills = [];
    private int sessionIndex;

    public override void Initialize()
    {
        settings = JsonDocument.Parse(File.ReadAllText("/tmp/ats/case.json")).RootElement;
        var dates = System.Linq.Enumerable.Select(settings.GetProperty("dates").EnumerateArray(), value => DateTime.Parse(value.GetString()!, CultureInfo.InvariantCulture)).ToArray();
        SetStartDate(dates[0]); SetEndDate(dates[^1]); SetTimeZone("Asia/Seoul");
        SetAccountCurrency("KRW"); SetCash(settings.GetProperty("initial_cash").GetDecimal());
        SetBrokerageModel(QuantConnect.Brokerages.BrokerageName.Default, AccountType.Cash);
        SetRiskFreeInterestRateModel(new ConstantRiskFreeRateInterestRateModel(0m));
        var security = AddData<SyntheticPoint>("KRXTEST", Resolution.Minute, NodaTime.DateTimeZoneProviders.Tzdb["Asia/Seoul"], fillForward: false, leverage: 1m);
        instrument = security.Symbol;
        security.SetFeeModel(new SyntheticFees(settings.GetProperty("buy_fee").GetDecimal(), settings.GetProperty("sell_fee").GetDecimal()));
        SetBenchmark(time => 1m);
    }

    public override void OnData(Slice slice)
    {
        if (!slice.ContainsKey(instrument)) return;
        var point = slice.Get<SyntheticPoint>(instrument);
        if (Time.TimeOfDay == TimeSpan.FromHours(9))
        {
            var lookback = settings.GetProperty("lookback").GetInt32();
            var target = history.Count >= lookback && history[^1] > history.TakeLast(lookback).Average()
                ? settings.GetProperty("quantity").GetDecimal() : 0m;
            if (settings.TryGetProperty("targets", out var targets)) target = targets[sessionIndex].GetDecimal();
            var delta = target - Portfolio[instrument].Quantity;
            if (delta != 0) MarketOrder(instrument, delta);
        }
        else if (Time.TimeOfDay == TimeSpan.FromHours(15.5))
        {
            history.Add(point.Value);
            equity.Add(Portfolio.TotalPortfolioValue);
            sessionIndex++;
        }
    }

    public override void OnOrderEvent(OrderEvent update)
    {
        if (update.Status == OrderStatus.Filled)
            fills.Add(new { date = Time.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
                side = update.FillQuantity > 0 ? "BUY" : "SELL", quantity = Math.Abs(update.FillQuantity),
                price = update.FillPrice, cost = update.OrderFee.Value.Amount });
    }

    public override void OnEndOfAlgorithm() => File.WriteAllText("/tmp/ats/result.json", JsonSerializer.Serialize(new {
        engine = "LEAN", version = "2.5.18127", full_backtest = true, certified = false, fills, equity
    }));
}

public class SyntheticFees(decimal buy, decimal sell) : FeeModel
{
    public override OrderFee GetOrderFee(OrderFeeParameters parameters) => new(new CashAmount(
        Math.Abs(parameters.Order.Quantity) * parameters.Security.Price *
        (parameters.Order.Direction == OrderDirection.Buy ? buy : sell), "KRW"));
}

public class OfflineResultHandler : QuantConnect.Lean.Engine.Results.BacktestingResultHandler
{
    public OfflineResultHandler() { RunResultsAnalysis = false; }
}