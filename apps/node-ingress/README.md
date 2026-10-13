# Node ingress

Отдельный static Go relay. В production слушает выбранные sockets; runtime
Agent остаётся в bridge. В development ingress выполняется только в Docker bridge,
без host ports/network, NET_ADMIN, TUN, Docker API или firewall commands.

Env: `INGRESS_ENVELOPE_FILE`, `INGRESS_SECRET_FILE`, `INGRESS_STATE_DIR`,
`INGRESS_SOCKET`; defaults `/etc/node/envelope.json`, `/run/node/ingress.secret`,
`/var/lib/node-ingress`, `/run/node/ingress.sock`. Socket0600, journal0600, directory0700.
`/node-ingress --healthcheck` выполняет только UDS dial, без создания auth sessions.

Wire DTO определены в `packages/contracts/awg_contracts/managed_node.py`.
Destinations берутся исключительно из immutable envelope RFC1918 backend IPv4 и
фиксированных ролей. Agent8443/solver ports запрещены. UDP flows имеют отдельный
connected backend socket, idle90s, общие node-wide TCP/UDP limits.

Auth handshake: GET `/v1/ingress/session` возвращает `session_id`, `challenge`
(hex32randombytes), `next_nonce:1`. TTL5min, максимум128 concurrent sessions.
Secret — bytes файла без surrounding ASCII whitespace, без hex decoding.
Requests: `X-Ingress-Session`, `X-Ingress-Nonce`, `X-Ingress-MAC` (hex SHA256 HMAC).
Canonical UTF8:

```
session_id\nchallenge\nMETHOD\npath\nnonce\nSHA256(raw_body)
```

GET body empty; JSON requests compact sorted JSON. Nonce строго увеличивается;
retry использует новую nonce. MAC comparison constant time, unknown/duplicate JSON
keys отвергаются. Errors `{code,message}` не содержат inputs/network exception text.

POST `/v1/ingress/validate` принимает bare desired DTO и временно проверяет bind:
watermark/journal не изменяет. Prepare принимает ingress prepare DTO; commit/abort
принимают ticket DTO. GET `/v1/ingress/state` возвращает actual DTO. Socket ACTIVE
не доказывает protocol health: ingress actual APPLYING/backend_readyFalse; Agent
подтверждает runtime health отдельно.

Prepare держит новые sockets без forwarding, accepted watermark fsync сохраняется
до ответа. Commit atomic rename+fsync сохраняет plan до activation; restart
восстанавливает только committed plan при совпадении envelope. Staged ticket после
restart отменяется, accepted watermark сохраняется. Повтор Prepare с тем же digest
выдаёт новый ticket. Exclusive state-directory flock исключает второго writer.

При смене backend для собственного public socket commit закрывает старые flows и
connections; generation guard не допускает завершение старого backend dial после
handoff. Same destination сохраняет сессии. Чужие sockets не отбираются.

Проверки: `docker build --target test -f apps/node-ingress/Dockerfile .`.
Runtime: `docker build -f apps/node-ingress/Dockerfile -t node-ingress .`.
