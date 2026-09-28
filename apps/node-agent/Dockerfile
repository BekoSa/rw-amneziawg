# AmneziaWG runtime pins. Update with scripts/update-amnezia.sh (it rewrites these four lines).
ARG AWG_GO_VERSION=v3.1.20260828
ARG AWG_GO_COMMIT=b5928efb6ca19f0153958460c3d141f04abc5c2e
ARG AWG_TOOLS_VERSION=v3.1.20260812
ARG AWG_TOOLS_COMMIT=ee0f0a9aa34ff0a0da4b3433b9512781cfe02843

FROM golang:1.25.7-bookworm AS go-builder
ARG AWG_GO_COMMIT
RUN git init /src && cd /src && git remote add origin https://github.com/amnezia-vpn/amneziawg-go.git && git fetch --depth 1 origin ${AWG_GO_COMMIT} && git checkout --detach FETCH_HEAD && test "$(git rev-parse HEAD)" = "${AWG_GO_COMMIT}"
WORKDIR /src
RUN CGO_ENABLED=0 go build -trimpath -o /out/amneziawg-go .

FROM debian:bookworm-slim AS tools-builder
ARG AWG_TOOLS_COMMIT
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential libmnl-dev && rm -rf /var/lib/apt/lists/*
RUN git init /src && cd /src && git remote add origin https://github.com/amnezia-vpn/amneziawg-tools.git && git fetch --depth 1 origin ${AWG_TOOLS_COMMIT} && git checkout --detach FETCH_HEAD && test "$(git rev-parse HEAD)" = "${AWG_TOOLS_COMMIT}"
RUN make -C /src/src WITH_BASHCOMPLETION=no WITH_WGQUICK=no

FROM python:3.12.12-slim-bookworm
ARG AWG_GO_VERSION
ARG AWG_GO_COMMIT
ARG AWG_TOOLS_VERSION
ARG AWG_TOOLS_COMMIT
LABEL org.amnezia.amneziawg-go.version=${AWG_GO_VERSION} org.amnezia.amneziawg-go.commit=${AWG_GO_COMMIT} \
      org.amnezia.amneziawg-tools.version=${AWG_TOOLS_VERSION} org.amnezia.amneziawg-tools.commit=${AWG_TOOLS_COMMIT}
ENV AWG_RUNTIME_VERSION="amneziawg-go ${AWG_GO_VERSION}, amneziawg-tools ${AWG_TOOLS_VERSION}"
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 nftables libmnl0 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=go-builder /out/amneziawg-go /usr/local/bin/amneziawg-go
COPY --from=tools-builder /src/src/wg /usr/local/bin/awg
COPY requirements.txt /tmp/requirements.txt
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.txt -c /tmp/requirements.lock
WORKDIR /app
COPY packages/contracts /app/packages/contracts
COPY packages/awg-config /app/packages/awg-config
COPY apps/node-agent/awg_agent /app/awg_agent
ENV PYTHONPATH=/app:/app/packages/contracts:/app/packages/awg-config PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN mkdir -p /run/amneziawg /var/lib/awg-agent && chmod 700 /var/lib/awg-agent
ENTRYPOINT ["python", "-m", "awg_agent"]
