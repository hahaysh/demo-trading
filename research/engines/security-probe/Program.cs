using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;
using System.Text.Json;
using System.Xml.Linq;

if (args[0] == "verify")
{
    foreach (var file in Directory.GetFiles(args[1], "*.dll"))
    {
        using var input = File.OpenRead(file);
        using var pe = new PEReader(input);
        if (!pe.HasMetadata) continue;
        var metadata = pe.GetMetadataReader();
        foreach (var handle in metadata.AssemblyReferences)
            if (metadata.GetString(metadata.GetAssemblyReference(handle).Name) == "DotNetZip")
                throw new InvalidOperationException($"Original vulnerable assembly reference remains: {Path.GetFileName(file)}");
    }
    if (File.Exists(Path.Combine(args[1], "DotNetZip.dll"))) throw new InvalidOperationException("Vulnerable DLL remains");
    System.Runtime.Loader.AssemblyLoadContext.Default.Resolving += (_, name) =>
    {
        var path = Path.Combine(args[1], name.Name + ".dll");
        return File.Exists(path) ? System.Reflection.Assembly.LoadFrom(path) : null;
    };
    var zipType = System.Reflection.Assembly.LoadFrom(Path.Combine(args[1], "ProDotNetZip.dll")).GetType("Ionic.Zip.ZipFile")!;
    var temporary = Path.Combine(Path.GetTempPath(), "ats-zip-regression-" + Guid.NewGuid());
    Directory.CreateDirectory(temporary);
    try
    {
        foreach (var member in new[] { "safe.txt", "../outside.txt", "../output-sibling/outside.txt" })
        {
            var archivePath = Path.Combine(temporary, "fixture.zip");
            using (var archive = System.IO.Compression.ZipFile.Open(archivePath, System.IO.Compression.ZipArchiveMode.Create))
            using (var writer = new StreamWriter(archive.CreateEntry(member).Open())) writer.Write("synthetic fixture");
            var output = Path.Combine(temporary, "output");
            Directory.CreateDirectory(output);
            using var archiveObject = (IDisposable)zipType.GetMethod("Read", new[] { typeof(string) })!.Invoke(null, new object[] { archivePath })!;
            try { zipType.GetMethod("ExtractAll", new[] { typeof(string) })!.Invoke(archiveObject, new object[] { output }); }
            catch (System.Reflection.TargetInvocationException) when (member != "safe.txt") { }
            if (Directory.GetFiles(temporary, "outside.txt", SearchOption.AllDirectories).Any(path => !Path.GetFullPath(path).StartsWith(output + Path.DirectorySeparatorChar, StringComparison.Ordinal)))
                throw new InvalidOperationException("ZIP extraction escaped destination");
            if (member == "safe.txt" && File.ReadAllText(Path.Combine(output, member)) != "synthetic fixture")
                throw new InvalidOperationException("Normal ZIP extraction failed");
            File.Delete(archivePath);
        }
    }
    finally { Directory.Delete(temporary, true); }
    Console.WriteLine("Normal and traversal ZIP regression passed with ProDotNetZip.");
    Console.WriteLine("No original DotNetZip binary or assembly reference in published output.");
    return;
}

var projects = new Dictionary<string, string>
{
    ["QuantConnect.Compression"] = "/lean/Compression/QuantConnect.Compression.csproj",
    ["QuantConnect.Common"] = "/lean/Common/QuantConnect.csproj",
    ["QuantConnect.Lean.Engine"] = "/lean/Engine/QuantConnect.Lean.Engine.csproj"
};
var locked = JsonDocument.Parse(File.ReadAllText("/runner/packages.lock.json")).RootElement.GetProperty("dependencies").EnumerateObject().Single().Value;
foreach (var project in projects)
{
    var document = XDocument.Load(project.Value);
    var root = document.Root!;
    root.Add(new XElement("PropertyGroup",
        new XElement("PackageId", project.Key), new XElement("Version", "2.5.18127"),
        new XElement("Description", "ATS development rebuild: upstream 03514bbc94fc391eb09ff0e592b262beb1e682fd; ProDotNetZip 1.19.0 replaces DotNetZip; published project dependencies are pinned.")));
    foreach (var reference in root.Descendants("PackageReference").ToArray())
    {
        if ((string?)reference.Attribute("Include") == "DotNetZip")
        {
            reference.SetAttributeValue("Include", "ProDotNetZip");
            reference.SetAttributeValue("Version", "1.19.0");
        }
        var version = (string?)reference.Attribute("Version");
        if (version is not null && !version.StartsWith('[')) reference.SetAttributeValue("Version", $"[{version}]");
    }
    foreach (var reference in root.Descendants("ProjectReference").ToArray())
    {
        var path = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(project.Value)!, reference.Attribute("Include")!.Value.Replace('\\', '/')));
        if (projects.Values.Contains(path)) continue;
        var package = Path.GetFileNameWithoutExtension(path);
        if (!locked.GetProperty(project.Key).GetProperty("dependencies").TryGetProperty(package, out var version))
        {
            Console.WriteLine($"Omitting unpublished reference from {project.Key}: {package}");
            reference.Remove();
            continue;
        }
        reference.ReplaceWith(new XElement("PackageReference", new XAttribute("Include", package), new XAttribute("Version", $"[{version.GetString()}]")));
    }
    document.Save(project.Value);
}
var runner = XDocument.Load("/runner/Runner.csproj");
var engine = runner.Descendants("PackageReference").Single(item => (string?)item.Attribute("Include") == "QuantConnect.Lean.Engine");
engine.ReplaceWith(new XElement("ProjectReference", new XAttribute("Include", projects["QuantConnect.Lean.Engine"])));
runner.Save("/runner/Runner.csproj");
File.WriteAllText("/runner/SECURITY-PATCH-NOTICE.txt", "Development rebuild of QuantConnect Compression, Common and Engine from commit 03514bbc94fc391eb09ff0e592b262beb1e682fd. Project dependencies changed to ProDotNetZip 1.19.0 and pinned published references. C# implementation source is unchanged. Not an upstream release or trading certification.\n");