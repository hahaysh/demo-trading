using System.Text.Json;
using System.Security.Cryptography;
using QuantConnect.Indicators;

decimal[] closes = [100m, 110m, 90m, 80m, 100m];
decimal[] expected = [100m, 105m, 100m, 85m, 90m];
var average = new SimpleMovingAverage(2);
var rows = new List<decimal[]>();
var signals = new List<bool>();
var start = new DateTime(2026, 9, 21, 6, 30, 0, DateTimeKind.Utc);
for (var index = 0; index < closes.Length; index++)
{
    average.Update(start.AddDays(index), closes[index]);
    var observed = average.Current.Value;
    if (observed != expected[index])
    {
        throw new InvalidOperationException("LEAN synthetic rolling feature differs from reference");
    }
    rows.Add([closes[index], observed]);
    signals.Add(average.IsReady && closes[index] > observed);
}
Console.WriteLine(JsonSerializer.Serialize(new
{
    kind = "LeanSyntheticIndicatorProbe",
    package_version = "2.5.18127",
    assembly_version = typeof(SimpleMovingAverage).Assembly.GetName().Version?.ToString(),
    dependency_lock_sha256 = Convert.ToHexString(
        SHA256.HashData(File.ReadAllBytes("/opt/probe/packages.lock.json"))).ToLowerInvariant(),
    mode = "SYNTHETIC_OFFLINE_ONLY",
    certified = false,
    rows,
    signals,
    broker_requests_sent = 0,
    evaluation_result_created = false,
    full_lean_backtest = false
}));