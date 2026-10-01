FROM mcr.microsoft.com/dotnet/sdk:10.0@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29 AS restore
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
WORKDIR /src
COPY Probe.csproj .
RUN dotnet restore --use-lock-file

FROM scratch AS lock
COPY --from=restore /src/packages.lock.json /packages.lock.json

FROM restore AS build
COPY packages.lock.json .
RUN dotnet restore --locked-mode
COPY Program.cs .
RUN dotnet publish --no-restore -c Release -o /out

FROM mcr.microsoft.com/dotnet/sdk:10.0@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29 AS probe
ENV HOME=/tmp DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_EnableDiagnostics=0
WORKDIR /opt/probe
COPY --from=build /out .
COPY packages.lock.json .
USER 65532:65532
ENTRYPOINT ["timeout", "60s", "dotnet", "Probe.dll"]