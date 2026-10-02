FROM mcr.microsoft.com/dotnet/sdk:10.0@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29 AS probe
ARG LEAN_SOURCE_SHA256=4f41ea42f3178c283a85909f2429548c0f55ca02a0b138072f6c811fadb16938
ADD --checksum=sha256:${LEAN_SOURCE_SHA256} https://codeload.github.com/QuantConnect/Lean/tar.gz/03514bbc94fc391eb09ff0e592b262beb1e682fd /tmp/lean.tar.gz
RUN mkdir /lean && tar -xzf /tmp/lean.tar.gz -C /lean --strip-components=1 && rm /tmp/lean.tar.gz
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
COPY security-probe/Patcher.csproj security-probe/Program.cs /patcher/
COPY Runner.csproj packages.lock.json LeanRunner.cs /runner/
RUN dotnet run --project /patcher/Patcher.csproj -- patch
COPY security-probe/locks/runner.lock.json /runner/packages.lock.json
COPY security-probe/locks/compression.lock.json /lean/Compression/packages.lock.json
COPY security-probe/locks/common.lock.json /lean/Common/packages.lock.json
COPY security-probe/locks/engine.lock.json /lean/Engine/packages.lock.json
RUN dotnet restore /runner/Runner.csproj --locked-mode -p:ImportDirectoryBuildProps=false
RUN dotnet publish /runner/Runner.csproj --no-restore -c Release -o /out -p:ImportDirectoryBuildProps=false
RUN dotnet run --no-build --project /patcher/Patcher.csproj -- verify /out
RUN cp /runner/packages.lock.json /runner/LeanRunner.cs /runner/SECURITY-PATCH-NOTICE.txt /out/ && cp /lean/LICENSE /out/LEAN-LICENSE && cp /root/.nuget/packages/prodotnetzip/1.19.0/LICENSE.txt /out/PRODOTNETZIP-LICENSE
RUN if [ -f /lean/NOTICE ]; then cp /lean/NOTICE /out/LEAN-NOTICE; fi
FROM scratch AS evidence
COPY --from=probe /runner/packages.lock.json /runner.lock.json
COPY --from=probe /lean/Compression/packages.lock.json /compression.lock.json
COPY --from=probe /lean/Common/packages.lock.json /common.lock.json
COPY --from=probe /lean/Engine/packages.lock.json /engine.lock.json
FROM mcr.microsoft.com/dotnet/sdk:10.0@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29 AS runtime
LABEL ats.security.upstream="03514bbc94fc391eb09ff0e592b262beb1e682fd" ats.security.patch="ProDotNetZip-1.19.0; development rebuild, not upstream release"
ENV HOME=/tmp DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_EnableDiagnostics=0
WORKDIR /opt/runner
COPY --from=probe /out .
USER 65532:65532
ENTRYPOINT ["timeout", "120s", "dotnet", "Runner.dll"]