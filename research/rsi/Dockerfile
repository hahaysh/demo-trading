ARG BASE=ats-backtest-engines:dev
FROM ${BASE}
COPY propose.py /opt/rsi/propose.py
USER 65532:65532
ENTRYPOINT ["timeout", "60s", "python", "/opt/rsi/propose.py"]