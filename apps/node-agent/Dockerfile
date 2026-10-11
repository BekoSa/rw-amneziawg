# AmneziaWG runtime pins. Update with scripts/update-amnezia.sh (it rewrites these four lines).
ARG AWG_GO_VERSION=v3.1.20260828
ARG AWG_GO_COMMIT=b5928efb6ca19f0153958460c3d141f04abc5c2e
ARG AWG_TOOLS_VERSION=v3.1.20260812
ARG AWG_TOOLS_COMMIT=ee0f0a9aa34ff0a0da4b3433b9512781cfe02843

FROM debian:bookworm-slim AS trusttunnel-binaries
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates build-essential libssl-dev libpcre2-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# Official release SHA256 pins. ?download=1 avoids a stale GitHub CDN error response.
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) tt_arch=x86_64; tt_sha=91c2ea3db7416a01b5258a4c047ec22890490bc55e1b194206031aa75144f0e7; lego_sha=2a35505089e7772c92e1e9ac144df91151ef2eca8568630db0ff91fca06d9bef ;; \
      arm64) tt_arch=aarch64; tt_sha=c2aee17a1ced349283cba4775202e2baba053b8ea835d4cc23dc67d16c6b9686; lego_sha=15b14ec2ab14fde69cc8396eb0204c5ce4327e31a486953225a6059b26db3e8c ;; \
      *) exit 1 ;; \
    esac; \
    curl --fail --location --retry 3 "https://github.com/TrustTunnel/TrustTunnel/releases/download/v1.1.0/trusttunnel-v1.1.0-linux-${tt_arch}.tar.gz?download=1" -o tt.tar.gz; \
    echo "$tt_sha  tt.tar.gz" | sha256sum -c -; \
    mkdir /out; tar -xzf tt.tar.gz --strip-components=1 -C /out "trusttunnel-v1.1.0-linux-${tt_arch}/trusttunnel_endpoint"; \
    curl --fail --location --retry 3 "https://github.com/go-acme/lego/releases/download/v5.5.2/lego_v5.5.2_linux_${TARGETARCH}.tar.gz?download=1" -o lego.tar.gz; \
    echo "$lego_sha  lego.tar.gz" | sha256sum -c -; tar -xzf lego.tar.gz -C /out lego; \
    chmod 755 /out/trusttunnel_endpoint /out/lego
# Compile on bookworm so the binary uses the same libc/OpenSSL ABI as the Agent.
RUN curl --fail --location --retry 3 https://www.haproxy.org/download/3.2/src/haproxy-3.2.5.tar.gz -o haproxy.tar.gz \
    && echo '77316a3e1b6c39245bc11ef58f4d6dadd063c014c1baec8f0d81798c519e072b  haproxy.tar.gz' | sha256sum -c - \
    && tar -xzf haproxy.tar.gz \
    && make -C haproxy-3.2.5 -j2 TARGET=linux-glibc USE_OPENSSL=1 USE_PCRE2=1 USE_PCRE2_JIT=1 \
    && cp haproxy-3.2.5/haproxy /out/haproxy

FROM python:3.12.12-slim-bookworm AS trusttunnel-toolchain
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 libpcre2-8-0 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=trusttunnel-binaries /out/ /usr/local/bin/
LABEL io.remnawave-extension.trusttunnel.version="1.1.0" io.remnawave-extension.lego.version="5.5.2" io.remnawave-extension.haproxy.version="3.2.5"
RUN trusttunnel_endpoint --version && lego --version && haproxy -v

FROM golang:1.25.7-bookworm AS go-builder
ARG AWG_GO_COMMIT
RUN git init /src && cd /src && git remote add origin https://github.com/amnezia-vpn/amneziawg-go.git && git fetch --depth 1 origin ${AWG_GO_COMMIT} && git checkout --detach FETCH_HEAD && test "$(git rev-parse HEAD)" = "${AWG_GO_COMMIT}"
WORKDIR /src
RUN CGO_ENABLED=0 GOMAXPROCS=2 go build -p 2 -trimpath -o /out/amneziawg-go .

FROM debian:bookworm-slim AS tools-builder
ARG AWG_TOOLS_COMMIT
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential libmnl-dev && rm -rf /var/lib/apt/lists/*
RUN git init /src && cd /src && git remote add origin https://github.com/amnezia-vpn/amneziawg-tools.git && git fetch --depth 1 origin ${AWG_TOOLS_COMMIT} && git checkout --detach FETCH_HEAD && test "$(git rev-parse HEAD)" = "${AWG_TOOLS_COMMIT}"
RUN make -C /src/src WITH_BASHCOMPLETION=no WITH_WGQUICK=no

FROM trusttunnel-toolchain
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
