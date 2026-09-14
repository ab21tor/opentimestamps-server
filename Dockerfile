# otsd runtime image — Python + dependencies ONLY. The calendar CODE is this
# checkout, mounted at /app at runtime, so code-only changes deploy with a
# pull + restart, no rebuild.
#
# A COPY (2026-09-11) of timestamp-gateway/otsd/Dockerfile for the appliance
# shape, where this checkout is the build context (docker-compose.enterprise.yml,
# "build: ."). The gateway's copy reads requirements.txt from a named "fork"
# build context instead; that is the only difference. Keep the two in step:
# the base image digest, the python-bitcoinlib pin, the CMD.
#
# Known-good resolved versions: opentimestamps 0.4.5, plyvel 1.5.1 (built
# from source against Debian 13's libleveldb 1.23), python-bitcoinlib 0.11.2.
# python:3.13-slim (2026-09-14): the 3.11 pin went with py-leveldb; plyvel
# reads the same LevelDB files, no calendar migration.
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

RUN apt-get update -qq && apt-get install -y -qq build-essential libleveldb-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -q -r /app/requirements.txt \
    && pip install -q python-bitcoinlib==0.11.2

VOLUME /calendar
EXPOSE 14788

# Bitcoin RPC config: single env var BITCOIN_RPC_SERVICE_URL (full URL
# including credentials), sourced from the gitignored .env — never on a
# command line, never in tracked files.
#
# "python /app/otsd", not "otsd": exec-form CMD resolves via PATH only, and
# the otsd script lives in the runtime mount, not on PATH. Binds localhost
# (otsd's default) — safe under any networking; the compose files override
# with --rpc-address 0.0.0.0 for their internal bridge network.
# --btc-max-fee takes BTC: 0.0002 BTC = 20,000 sats. Never "fix" it to 20000.
# No -v: INFO is the production log level in every launch path — DEBUG
# ships commitment hexes and wallet outpoints into container logs.
# otsd's clean-exit path hangs off KeyboardInterrupt: SIGINT, not the
# docker-default SIGTERM, is what actually runs it.
STOPSIGNAL SIGINT
CMD ["python", "/app/otsd", "--calendar", "/calendar", "--btc-conf-target", "12", "--btc-max-fee", "0.0002"]
