# Управление нодой из панели после однократной установки

Дата: 2026-10-12. Статус: проект спецификации для review пользователя.
Документ описывает решение; реализация и план реализации ещё не утверждены.

## 1. Требование и границы

Пользователь устанавливает ноду один раз. Затем через существующий TUI расширения
управляет AWG, TrustTunnel, портами и сертификатами, без повторного входа на сервер
для изменения Docker Compose, environment или включения протокола. Требуется
выбор любого свободного порта, а не заранее опубликованного диапазона.
На сервере остаётся обслуживание образов: pull и recreate контейнеров.

Управление относится к самой ноде. Изменение firewall провайдера, host firewall,
DNS-записей и конфигурации stock Remnawave в объём задачи не входит. Bind сокета
не означает, что внешний firewall разрешает входящий трафик. Панель показывает
эти состояния отдельно и не обещает доступность непроверенного endpoint.

Stock Remnawave, его Node, Xray и база данных остаются внешними зависимостями.
Контроллер использует существующие публичные API, хранит свои данные в отдельной
БД, не изменяет upstream. Новая веб-панель не создаётся: административный поток
работает через нынешний TUI и Controller API.

## 2. Рассмотренные подходы

| Подход | Свободный произвольный порт | Последствия |
|---|---|---|
| Заранее опубликованный TCP/UDP-пул | Только внутри пула | Сохраняет нынешний bridge, но не выполняет выбранное пользователем требование |
| Host networking всего Agent либо Docker supervisor | Да | AWG NET_ADMIN в host namespace опасен; Docker API даёт слишком широкие полномочия для пересоздания mappings |
| Отдельный ограниченный host ingress, Agent в bridge | Да | Дополнительный relay и ресурсный контракт; сохраняет изоляцию AWG и не требует Docker API |

Выбран третий подход. Host networking применяется только к production ingress,
который занимается сокетами и передачей данных. В development и tests этот же
компонент запускается в bridge-сети. Production manifests не применяются к
development-host.

## 3. Компоненты и полномочия

```text
TUI → Controller → mTLS Agent API
                       │
                       ├─ AWG userspace runtime, routes/NAT в bridge namespace
                       ├─ TrustTunnel + HAProxy + ACME в bridge namespace
                       └─ ограниченный локальный ingress control API
                                  │
Internet → production host ingress ─┴→ фиксированный private Agent IP
```

Основной Agent сохраняет bridge networking, свою PKI, state volume и необходимые
для AWG контейнерные NET_ADMIN/TUN. Он не приобретает host networking или доступ
к host routes/firewall. Image содержит поддерживаемые runtime binaries независимо
от включённых профилей. «Протокол установлен» и «протокол активен» — разные поля.

Ingress — отдельный специализированный процесс в контейнере:

- production `network_mode: host`, `cap_drop: ALL`, только `NET_BIND_SERVICE`;
- без NET_ADMIN, NET_RAW, TUN, privileged, Docker socket/API и host filesystem;
- read-only root filesystem, ограниченные tmpfs, no-new-privileges;
- небольшой named volume для собственного журнала и локального control socket;
- передаёт только TCP/UDP объявленных bindings в фиксированный Agent backend;
- не выполняет shell-команды, не конфигурирует host interfaces/routes/sysctl/nft;
- не является proxy к management API, произвольному адресу или URL.

Ingress обладает доступом к host network namespace для bind, поэтому ограничения
назначения и control API обязательны даже при отсутствии NET_ADMIN. Его compromise
не должен давать возможность запрашивать forwarding к localhost, host management
services или произвольным private destinations.

## 4. Install envelope и границы доверия

Однократная установка создаёт immutable `NodeInstallEnvelope`:

- node UUID и identity control plane;
- фиксированный private IPv4 Agent в проектной Docker bridge-сети;
- разрешённые внешние bind addresses: по умолчанию wildcard IPv4/IPv6;
- management endpoint, его host port и зарезервированные control-plane ports;
- допустимые protocol backend listener roles, без Agent management listener;
- версии ingress control protocol и ограничения ресурсов;
- идентификатор envelope и digest для проверки обоими контейнерами.

Backend IP задаётся установщиком и не редактируется профилем в TUI. Agent recreate
сохраняет private IP. Миграция subnet/IP — отдельная provisioning-операция с новой
версией envelope; несовпадение fail-closed, а не автоматическое принятие адреса,
сообщённого любой стороной. Установка сначала проверяет конфликт Docker subnet.

Agent и ingress используют локальный UDS в отдельном named volume, Unix permissions
и отдельный install-generated control credential. Credential не появляется в
Controller API или логах; HMAC authenticated requests связывают method, body digest,
envelope ID и nonce. Повторный одинаковый Apply остаётся идемпотентным через journal;
nonce replay отвергается в пределах текущей control session. После рестарта новая
challenge/session исключает воспроизведение запросов старой сессии. UDS не публикуется
по TCP. Ingress проверяет envelope, node identity и роль каждого backend.

Передавать произвольные backend IP, команды, файлы, container IDs или Docker options
через этот API запрещено. Typed backend role разрешает только объявленный Agent
listener и не может ссылаться на management 8443, control UDS или internal solver
в обход предусмотренной ACME ingress цепочки.

## 5. Контракты и версия

Новый ресурсный интерфейс вводится отдельно от существующих AWG/TT v1 моделей:
`node.remnawave-extension.io/v1`. Endpoint negotiation публикует поддерживаемые
node/ingress API versions и capabilities. Старый Controller не получает новые поля
в strict v1 DTO; новый Controller определяет legacy Agent и показывает отсутствие
managed-ingress capability. Неизвестная версия запрещает node Apply с понятным кодом.

Основные DTO:

| DTO | Назначение |
|---|---|
| `NodeInstallEnvelope` | Неподконтрольные profile API границы установленной ноды |
| `NodeCapabilities` | Установленные protocol adapters, dynamic ingress, transports, IPv6 и resource limits |
| `NodeDesiredConfig` | Node UUID, монотонная revision, envelope digest, enabled protocols и полный listener plan |
| `IngressBinding` | Binding UUID, deployment UUID/role, transport, public bind/port, backend role/port |
| `NodeValidationResult` | Revision/digest, capabilities/envelope checks, binding conflicts, предупреждения внешней доступности |
| `NodeActualState` | Accepted/applied/LKG revision, digest, runtime epoch, bindings, состояние и sanitized issue |

Desired хранится Controller в собственной БД. Agent владеет durable accepted
watermark, локальным ресурсным журналом и координацией runtime. Ingress владеет
журналом фактически удерживаемых sockets. ACME accounts, private keys и certificates
остаются на Agent; ingress не получает credentials пользователей или TLS keys.

Каждый protocol desired ссылается на node resource revision. AWG и TT deployment
revisions сохраняют независимость. Node allocation lock сериализует изменения
портов и protocol deployments; stale writer защищён expected applied revision.
Node revision нельзя переиспользовать с другим digest. Digest включает весь
канонический ресурсный план и envelope reference.

Пустой legacy AWG `listen_ports` не трактуется как подтверждение доступности всех
host ports. Для legacy-ноды dynamic ingress отсутствует; текущая функциональность
работает по старым правилам до одноразовой миграции.

## 6. Слушатели и relay

Панель может выбрать любой порт 1–65535, если он не занят, не зарезервирован
control-plane envelope и не конфликтует с другим active binding. TCP и UDP одного
номера — отдельные ресурсы. IPv4/IPv6 wildcard overlap учитывается по реальному bind;
IPv6 sockets используют explicit IPV6_V6ONLY. SO_REUSEPORT и разделение listener
с чужим процессом запрещены. Ingress не убивает владельца занятого порта.

Validate выполняет semantic checks и временный bind probe; результат может устареть.
Apply вновь резервирует sockets и только после успешной reservation меняет runtime.
Конфликт возвращает `PORT_IN_USE`; операция не затрагивает Remnawave/Xray.

TCP relay передаёт байты без TLS termination и PROXY protocol. Agent HAProxy
продолжает различать ACME TLS-ALPN ClientHello и обычный TrustTunnel трафик.
HTTP-01 приходит в предусмотренный HTTP ingress Agent. Обычный трафик не попадает
на solver ports. Half-close, backpressure, deadlines и ограничение connections
являются частью поведения relay.

UDP relay ведёт отдельный source-flow mapping для каждого client IP/port и binding.
Каждый flow имеет отдельный connected backend socket: это сохраняет однозначное
направление ответов AWG/QUIC. Входящие backend datagrams принимаются только от
фиксированного destination socket. TTL idle flows, maximum concurrent flows,
datagram size и queue limits ограничены install capability. Лимиты возвращаются
в metrics без credentials/high-cardinality client identifiers. Flow не переносится
между deployments при повторном использовании public port.

Не обещается прозрачное сохранение исходного client IP внутри Agent: runtime видит
relay endpoint. AWG identity остаётся cryptographic peer identity, TT identity —
проверенные credentials. QUIC migration допускает создание нового source flow,
но проверяется настоящим клиентом. Нельзя считать этот relay работающим только
по успешному bind или сгенерированному конфигу.

## 7. Apply, rollback и recovery

```text
DRAFT → VALIDATING → VALIDATED → RESERVING → APPLYING → READY
                                  └──────────→ ROLLING_BACK → DEGRADED/ERROR
```

Agent записывает accepted revision/digest до mutation. Подготавливает backend,
резервирует новые ingress sockets, проверяет локальный runtime и фиксирует commit
ресурсного плана. Ingress начинает forwarding только committed bindings. ОС не
даёт атомарного bind нескольких ports: journal и reservation обеспечивают recovery,
а external READY публикуется только после завершения всего плана.

При смене порта новый путь подготавливается до удаления старого. Старые TCP sessions
дренируются ограниченное время, если не требуется отзыв доступа. Старые UDP flows
закрываются при снятии binding. При замене протокола на том же UDP-порту безопасный
make-before-break невозможен: панель предупреждает о кратком перерыве, операция
сериализуется, rollback проверяет возможность восстановить прежний bind.

Новый endpoint публикуется в подписках только после подтверждения applied node
revision и protocol READY. Полностью сохранённый старый runtime может оставаться
доступным по старому actual endpoint; desired port сам по себе не публикуется.
Промежуточная либо filtered rollback конфигурация получает DEGRADED, applied revision
не приравнивается к desired. Ни rollback, ни восстановление ingress не возвращают
disabled/deleted peers, rotated credentials или старые entitlements. Для TT отзыв
закрывает соответствующие runtime sessions; сохранение relay sockets не даёт права
оставить авторизованную старую сессию.

После crash Agent и ingress сверяют envelope, accepted watermark, commit record
и observed sockets. Неcommitted staged bindings удаляются; committed plan
восстанавливается и проверяется до READY. Stale/duplicate Apply не создаёт второй
listener или вторую revision. При недоступном Controller работающий committed plan
сохраняется. Отказы backend не превращаются в forwarding на другой destination.

Host bind readiness, backend health и externally probed connectivity отображаются
отдельно. Недоступность внешней проверки не маскируется как подтверждённая
connectivity. Подписка требует healthy committed protocol path, а явно выявленный
ingress failure блокирует новый endpoint. Недоступная проверка имеет UNKNOWN статус,
а не искусственный успех или ошибку сертификата.

## 8. ACME и конфликты 80/443

Настройки domain, email, environment и challenge редактируются в TUI. Agent
самостоятельно выпускает и обновляет сертификаты, сохраняет accounts и backoff.
HTTP-01 требует публичный TCP80, TLS-ALPN-01 — публичный TCP443; смена TT data port
не меняет эти требования. Порты ACME — отдельные роли node listener plan.

Если 443 уже занят rw-core, нода не пытается захватить его или изменить Xray.
TrustTunnel может работать на другом свободном TCP/UDP-порту, а ACME — через
свободный TCP80. Если и 80 недоступен, панель возвращает понятное ограничение.
Auto выбирает только реально разрешённые ingress methods и сохраняет прежнюю
безопасную классификацию fallback/backoff; ошибка account/rate limit не вызывает
произвольное переключение.

DNS-01, DNS provider credentials и delegated ACME hosting в эту реализацию не
входят. Их нельзя объявить fallback для занятого 80/443 без отдельного дизайна.
Публичные A/AAAA должны вести на ноду; внешние DNS/firewall меняет оператор.
Staging certificates не публикуются в пользовательские подписки.

## 9. TUI и диагностика

Карточка ноды показывает installed/enabled protocols, desired/applied resource
revision, реальные TCP/UDP bindings, ACME доступность и результаты external probes.
Порт редактируется в профиле; Controller формирует связанный node allocation plan.
Save остаётся draft, Validate ничего постоянно не применяет, Apply запускает
revision workflow. Для неподготовленной legacy-ноды показывается migration required.

Ошибки разделяются: `PORT_IN_USE`, `RESERVED_CONTROL_PORT`, `BACKEND_NOT_READY`,
`INGRESS_UNAVAILABLE`, `ENVELOPE_MISMATCH`, `UNSUPPORTED_NODE_API`,
`ACME_INGRESS_UNAVAILABLE`. Общий HTTP503 сохраняется только для действительно
недоступной зависимости и сопровождается sanitized component/operation code.
Сообщения не содержат credentials, приватные ключи, raw process stderr или config.

## 10. Одноразовая миграция существующих нод

Обычный image pull не добавит новый Compose service и не уберёт нынешние mappings.
Для установленной AWG-ноды требуется один запуск обновлённого migration installer.
Он сохраняет PKI, node UUID, AWG server/client identity, state volumes, management
port, текущие AWG/TT endpoints и существующие deployed revisions.

Preflight проверяет state backup, supported versions, subnet/IP и текущий портовый
план. Исходные Compose/env сохраняются для rollback. Agent bridge получает stable
private IP; ingress service добавляется production manifest. Перенос published
data ports к ingress требует освобождения старых Docker mappings и recreation
Agent, поэтому обещать отсутствие краткого перерыва при миграции нельзя. Management
mapping сохраняется; AWG NAT/routes продолжают жить только внутри Agent namespace.

После migration runtime восстанавливается из durable state, ingress открывает
те же внешние порты, Controller сверяет actual и identity. При неуспехе migration
восстанавливает прежний Compose/env и исходные endpoints. Нельзя заменять ключи
или произвольно переназначать порт ради автоматического завершения миграции.

Дальнейшая смена протоколов/портов не требует SSH. Обновление версий выполняется
pull и recreate через установленный Compose; compatibility preflight не принимает
неизвестные node/ingress версии молча. Destructive state migration не входит в
обычный restart и требует самостоятельного backup/restore процесса.

## 11. Проверки и acceptance

Все development services и test clients запускаются в Docker bridge. Production
host-network manifest проверяется структурно, но никогда не применяется к
development-host. Реальный production bind дополнительно проверяется на отдельном
подготовленном disposable runner; без этого результат bridge lab не объявляется
production host-network verification.

Обязательные проверки:

1. Contract version negotiation, strict DTO, digest, stale/duplicate revision,
   envelope mismatch, forbidden backend targets и management forwarding denial.
2. Реальные TCP payloads и half-close, UDP flow isolation/limits/expiry, IPv4/IPv6
   port conflicts, wildcard overlap, запрет shared port takeover.
3. Реальные AWG handshake и TCP/UDP через ingress, full-tunnel внутри test client;
   TT HTTP2/QUIC и native client TCP/UDP с TLS verification включённым.
4. HTTP-01/TLS-ALPN-01, fragmented ClientHello, auto fallback, renewal и recovery,
   occupied 80/443 без вмешательства в имитируемый stock listener.
5. Изменение произвольного порта через TUI/Controller без compose mutation;
   отказ между reservation/commit/activation, Agent/ingress restart и reconnect,
   безопасный rollback после revoke/rotation, закрытие прежних sessions.
6. Миграция из нынешнего AWG-only и TT-enabled Compose: сохранение identity,
   восстановление peers/endpoints/state, failure rollback и отсутствие второго
   неконтролируемого listener после cleanup.
7. Unit/contract/subscription golden suites; подписки не публикуют неподтверждённый
   desired endpoint и сохраняют stock fallback при недоступном расширении.
8. Network safety lint и host baseline до/после: host VPN, routes, policy rules и
   firewall не меняются project-specific командами; dev host networking запрещён.
9. Removal/restore: stock Remnawave работает до, без и после расширения; ingress
   освобождает только собственные sockets, extension volumes сохраняются.
10. Независимый security/architecture reviewer перед завершением реализации.

Успех означает: после однократной подготовленной установки оператор меняет любой
свободный незарезервированный порт и включает поддерживаемый протокол из TUI;
actual подтверждает работающий путь реальным трафиком; stock и host VPN остаются
сохранны. Требование не распространяется на обход внешнего firewall, захват чужого
порта или сертификаты без доступного стандартного ACME ingress.
