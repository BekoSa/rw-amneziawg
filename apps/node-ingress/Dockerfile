FROM golang:1.24.13-bookworm@sha256:1a6d4452c65dea36aac2e2d606b01b4a029ec90cc1ae53890540ce6173ea77ac AS source
WORKDIR /src
COPY apps/node-ingress/ ./
ENV CGO_ENABLED=0 GOMAXPROCS=2
FROM source AS test
RUN CGO_ENABLED=1 go test -race -p 1 ./...
FROM source AS build
ARG TARGETOS=linux
ARG TARGETARCH
RUN GOOS=$TARGETOS GOARCH=$TARGETARCH go build -p 1 -trimpath -ldflags='-s -w' -o /node-ingress .
FROM scratch AS runtime
COPY --from=build /node-ingress /node-ingress
USER 0:0
ENTRYPOINT ["/node-ingress"]
