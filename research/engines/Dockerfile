ARG LEAN_IMAGE=ats-local-build:required
ARG QLIB_IMAGE=ats-local-build:required
FROM ${LEAN_IMAGE} AS lean
FROM ${QLIB_IMAGE}
COPY --from=lean /usr/share/dotnet /usr/share/dotnet
COPY --from=lean /opt/runner /opt/runner
ENV PATH="/usr/share/dotnet:${PATH}" DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_EnableDiagnostics=0 DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 DOTNET_SYSTEM_GLOBALIZATION_PREDEFINED_CULTURES_ONLY=0
USER 65532:65532
ENTRYPOINT ["timeout", "120s", "python", "/opt/qlib_run.py"]