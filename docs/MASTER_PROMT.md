# Remnawave AmneziaWG Extension

## 0. Язык работы

Всегда отвечай пользователю и главному агенту на русском языке.

Код, названия протоколов, API, поля конфигурации, идентификаторы, имена библиотек и общепринятые технические термины можно оставлять на английском, если так точнее.

Документацию проекта предпочтительно писать на русском, кроме случаев, когда конкретный технический артефакт должен быть на английском.

---

# 1. Главная цель проекта

Необходимо разработать production-ready расширение для Remnawave, которое добавляет **AmneziaWG как полноценный управляемый пользовательский способ подключения** рядом с существующими Xray-протоколами.

С точки зрения администратора должно быть возможно:

1. развернуть обычный Remnawave;
2. развернуть AWG Agent на нужной ноде;
3. зарегистрировать эту ноду в AWG Controller;
4. создать AWG Profile;
5. указать endpoint, IP pool и параметры AmneziaWG;
6. выбрать пользователей или Internal Squads Remnawave;
7. выполнить validation;
8. нажать Apply;
9. получить готовую работающую AWG-ноду;
10. автоматически создать peers для соответствующих пользователей;
11. автоматически добавлять AWG в пользовательские подписки.

Идеальный административный workflow:

```text
Remnawave Node существует
        ↓
AWG Agent зарегистрирован
        ↓
Create AWG Profile
        ↓
Validate
        ↓
Apply
        ↓
Node READY
        ↓
Remnawave users/squads
        ↓
AWG peers автоматически созданы
        ↓
Subscription обновилась
        ↓
Пользователь видит:
VLESS / XHTTP / Reality / AWG
```

---

# 2. Ключевая архитектурная цель

Расширение должно быть максимально независимо от внутреннего устройства Remnawave.

Нужно добиться следующего свойства:

> Remnawave можно обновлять обычными официальными Docker images без необходимости постоянно переносить крупный fork.

И второго свойства:

> AWG-расширение можно полностью отключить или удалить, после чего stock Remnawave продолжит работать как раньше.

И третьего:

> После повторного включения AWG-расширения его собственные данные сохраняются, reconciliation восстанавливает peers и работа продолжается.

---

# 3. Stock Remnawave является внешней системой

Remnawave рассматривается как внешний upstream dependency.

Нельзя превращать этот проект в глубокий fork Remnawave.

## Запрещено

Никогда:

* не изменяй schema PostgreSQL Remnawave;
* не добавляй AWG-specific поля в таблицы Remnawave;
* не выполняй прямые SQL-запросы к PostgreSQL Remnawave;
* не используй внутреннюю структуру БД Remnawave как API;
* не изменяй исходный backend Remnawave;
* не модифицируй Remnawave Node;
* не заменяй stock Remnawave Node кастомным Node image;
* не добавляй AWG внутрь Xray только ради этого проекта;
* не требуй кастомного Xray-core для базовой архитектуры;
* не изменяй stock Remnawave container вручную после запуска;
* не делай `docker exec` с последующим patch application files как часть установки.

Интеграция с Remnawave должна выполняться через публичные и документированные механизмы:

* HTTP API;
* webhooks;
* стабильные UUID сущностей;
* официально поддерживаемые response/subscription механизмы.

Если некоторой функциональности нет в официальном API, нельзя автоматически компенсировать это прямым доступом к базе данных.

---

# 4. Source of truth

## Remnawave является source of truth для:

* identity пользователя;
* UUID пользователя;
* enable/disable;
* expiration;
* lifecycle;
* membership в squads;
* основных subscription identities;
* Xray nodes;
* Xray traffic, который собирает сам Remnawave.

## AWG Extension является source of truth только для:

* AWG profiles;
* AWG nodes;
* AWG agent capabilities;
* AWG client keys;
* AWG server public keys;
* peer assignments;
* AWG IP allocations;
* AWG revisions;
* AWG actual state;
* AWG traffic counters;
* AWG-specific subscription representation.

Не создавай вторую независимую identity system пользователей.

Связь между системами строится через стабильный:

```text
remnawave_user_uuid
```

---

# 5. Компоненты системы

Проект должен быть разделён минимум на четыре логических компонента.

```text
                    ┌─────────────────────┐
                    │   Stock Remnawave   │
                    │ Users / Squads      │
                    │ Limits / Xray Nodes │
                    └──────────┬──────────┘
                               │
                         API + Webhooks
                               │
                               ▼
                    ┌─────────────────────┐
                    │   AWG Controller    │
                    │ Desired state       │
                    │ DB / IPAM / Keys    │
                    └───────┬───────┬─────┘
                            │       │
                     mTLS/API       │
                            │       │
                            ▼       ▼
                    ┌──────────┐  ┌─────────────────┐
                    │AWG Agent │  │Subscription GW  │
                    │AmneziaWG │  │render/enrichment│
                    └──────────┘  └─────────────────┘
                            │
                            ▼
                        Internet
```

Дополнительно:

```text
AWG Web UI
     ↓
AWG Controller API
```

---

# 6. Структура репозитория

Предпочтительная структура:

```text
/apps
  /controller
  /node-agent
  /subscription-gateway
  /web-ui

/packages
  /contracts
  /remnawave-client
  /awg-config
  /subscription-renderers
  /capabilities

/deploy
  /dev
  /production

/tests
  /unit
  /integration
  /e2e
  /network
  /golden

/docs
  MASTER_PROMPT.md
  architecture.md
  /decisions
```

Controller ↔ Agent contracts должны быть versioned.

Предпочтительно использовать:

* OpenAPI;
* protobuf/gRPC;
* другой строгий versioned contract.

Не создавай implicit API, существующий только в реализации клиента и сервера.

---

# 7. AWG Controller

Controller является центральным control plane AWG.

Он должен:

* обращаться к публичному API Remnawave;
* принимать webhooks Remnawave;
* выполнять периодический reconciliation;
* иметь собственную PostgreSQL;
* управлять AWG Profiles;
* управлять AWG Nodes;
* регистрировать Agents;
* получать Agent capabilities;
* генерировать client keypairs;
* хранить client private keys encrypted-at-rest;
* выполнять IP allocation;
* вычислять desired state;
* отправлять desired state Agent;
* получать actual state;
* управлять config revisions;
* собирать AWG peer traffic;
* отдавать данные Subscription Gateway;
* предоставлять API для UI.

Controller должен быть идемпотентным.

Повторное выполнение reconciliation не должно создавать новые peers, новые IP или новые ключи без необходимости.

---

# 8. Webhooks не являются source of truth

Webhook нужен для быстрой реакции.

Но система не должна зависеть от гарантированной доставки каждого webhook.

Неправильно:

```text
webhook пришёл
→ применили изменение

webhook потерялся
→ permanent desync
```

Правильно:

```text
                  webhook
Remnawave ─────────────────→ Controller
     │
     │ periodic reconciliation
     └─────────────────────→ Controller
```

Controller регулярно получает актуальный state Remnawave и сравнивает его со своим desired state.

Пример:

```text
Desired:
Alice ACTIVE
Alice должен иметь AWG DE + NL

Actual:
Alice имеет AWG DE

Action:
создать peer на NL
```

После рестарта Controller должен автоматически восстановить актуальное состояние.

---

# 9. Lifecycle пользователей

Controller должен корректно обрабатывать как минимум:

* user created;
* user modified;
* user enabled;
* user disabled;
* user limited;
* user expired;
* user traffic reset;
* user deleted;
* squad membership change.

При:

```text
disabled
expired
limited
deleted
```

AWG access должен быть отключён согласно выбранной policy.

При:

```text
enabled
```

peer должен быть восстановлен автоматически.

Удалённый пользователь не должен исчезать из control plane мгновенно, если существуют offline nodes.

Используй tombstone/deletion reconciliation, чтобы при возвращении временно offline Agent удалённый peer гарантированно был удалён.

---

# 10. Internal Squads как entitlement

Предпочтительная модель доступа:

AWG Profile может указывать набор Remnawave Internal Squads.

Например:

```yaml
access:
  squadIds:
    - UUID_PREMIUM
    - UUID_AWG
```

Если пользователь входит в нужный squad:

```text
desired AWG peer = PRESENT
```

Если вышел:

```text
desired AWG peer = ABSENT
```

Controller не должен копировать squad system целиком.

---

# 11. Собственная AWG Database

Используй отдельную PostgreSQL.

Пример доменных таблиц:

```text
awg_profiles
awg_nodes
awg_agents
awg_users
awg_peers
awg_ip_allocations
awg_config_revisions
awg_peer_stats
awg_tombstones
```

Не обязательно использовать именно эти имена, но domain separation должна быть явной.

Связь с Remnawave:

```text
remnawave_user_uuid
remnawave_node_uuid
remnawave_squad_uuid
```

---

# 12. IPAM

Реализуй собственный transactional IPAM.

Требования:

* IPv4;
* optional IPv6;
* unique constraints;
* transactional allocation;
* race-safe allocation;
* pool exhaustion detection;
* deterministic cleanup;
* возможность quarantine освобождённого IP перед повторным использованием;
* невозможность назначить один IP двум активным peers.

Пример:

```text
10.81.0.1     server
10.81.0.2     user A
10.81.0.3     user B
...
```

При параллельном создании пользователей не должно возникать duplicate allocations.

---

# 13. Ключи клиентов

Допустим и предпочтителен режим:

```text
один AWG client keypair
на одного пользователя
```

с использованием одного public key пользователя на нескольких нодах.

Например:

```text
Alice public key
   ├── DE
   ├── NL
   └── FI
```

Это должно быть configurable architectural choice, а не случайный side effect.

Client private key:

* генерируется безопасно;
* хранится encrypted-at-rest;
* никогда не логируется;
* не попадает в tracing;
* не попадает в Prometheus labels;
* не выводится в error reports.

---

# 14. Server private key

Server private key никогда не должен отправляться Controller.

Он генерируется и хранится только на AWG Agent/node.

Например:

```text
/var/lib/remnawave-awg-agent/keys/server.key
```

Controller получает только:

```text
serverPublicKey
```

Если Agent переустанавливается, должна существовать понятная migration/recovery policy.

---

# 15. AWG Node Agent

Agent является data-plane manager для AmneziaWG.

Он запускается отдельно от Remnawave Node.

На production-сервере концептуально:

```text
server
│
├── official Remnawave Node
│      └── Xray-core
│
└── AWG Agent
       └── AmneziaWG
```

Они не должны зависеть друг от друга на уровне процесса.

Agent должен уметь:

* register;
* authenticate Controller;
* сообщать capabilities;
* валидировать profile/config;
* компилировать runtime config;
* применять revision;
* выполнять atomic apply;
* сохранять last-known-good revision;
* rollback;
* возвращать appliedRevision;
* сообщать interface state;
* сообщать server public key;
* сообщать listen port;
* создавать peer;
* изменять peer;
* удалять peer;
* собирать RX;
* собирать TX;
* собирать latest handshake;
* работать идемпотентно;
* восстанавливаться после restart.

---

# 16. AWG Runtime

Предпочтительно поддерживать абстракцию драйверов:

```text
AWGRuntime
  ├── UserspaceAWGRuntime
  └── KernelAWGRuntime
```

Development и CI должны использовать userspace implementation, например `amneziawg-go`.

Production позже может поддерживать kernel backend.

Не связывай весь Controller с конкретным способом запуска AmneziaWG.

---

# 17. Agent capabilities

Controller не должен предполагать, что каждая AWG-нода умеет все версии протокола.

Agent должен сообщать capabilities.

Пример:

```yaml
awg:
  versions:
    - "2"
    - "3"
    - "3.1"

runtime:
  userspace: true
  kernel: false

features:
  iFields: true
  advancedJunk: true
  randomTrailers: true
```

Profile требует определённые features.

Controller должен выполнить validation:

```text
Profile requirements
        ↓
Agent capabilities
        ↓
compatible / incompatible
```

Если нода не поддерживает выбранную версию:

```text
Apply запрещён
```

В UI должна отображаться понятная причина.

---

# 18. AWG Profile

Используй versioned schema.

Пример концепции:

```yaml
apiVersion: awg.remnawave-extension.io/v1
kind: AWGProfile

metadata:
  id: ...
  name: Germany-AWG

spec:
  endpoint:
    host: de.example.com
    port: 51820

  network:
    ipv4Pool: 10.81.0.0/24
    ipv6Pool: fd81::/64
    mtu: 1420

  protocol:
    version: "3.1"
    parameters:
      ...

  dns:
    servers:
      - 1.1.1.1

  access:
    squadIds:
      - ...

  nodeSelector:
    ...
```

Не размазывай AWG version-specific fields по всей кодовой базе.

Используй version adapters/schema adapters.

---

# 19. Validation pipeline

Нажатие Save не должно мгновенно уничтожать работающий tunnel.

Используй последовательность:

```text
edit
 ↓
schema validation
 ↓
semantic validation
 ↓
IP pool validation
 ↓
endpoint validation
 ↓
Agent capabilities validation
 ↓
dry-run / compile
 ↓
revision creation
 ↓
apply
 ↓
health check
 ↓
READY
```

При ошибке:

```text
rollback → last known good
```

---

# 20. Revision model

Для config state используй минимум:

```text
desiredRevision
validatedRevision
appliedRevision
lastKnownGoodRevision
```

Например:

```text
desired = 52
validated = 52
applied = 51
```

Нода ещё не готова.

Только когда:

```text
desiredRevision == appliedRevision
```

и health checks успешны:

```text
state = READY
```

---

# 21. Subscription не должна содержать неготовую AWG-ноду

Subscription Gateway добавляет AWG endpoint только если:

```text
Node enabled
AND
Agent online
AND
Profile valid
AND
desiredRevision == appliedRevision
AND
health == READY
```

Не отдавай пользователям endpoint во время rollout.

---

# 22. Controller ↔ Agent security

Management connection должна использовать mutual authentication.

Предпочтительно:

```text
mTLS
```

или эквивалентный механизм.

Agent management API не должен быть публичным unauthenticated endpoint.

Разделяй:

```text
management API
health API
metrics API
```

При необходимости health endpoint может иметь другую policy.

---

# 23. Subscription Gateway

Gateway располагается перед stock Remnawave subscription endpoint.

Схема:

```text
Client
  ↓
Subscription Gateway
  ↓
Stock Remnawave subscription endpoint
  ↓
original response
  ↓
AWG enrichment
  ↓
Client
```

Основной принцип:

> Не переизобретать subscription logic Remnawave.

Remnawave должен продолжать отвечать за свою стандартную подписку.

Gateway только добавляет AWG.

---

# 24. Subscription Gateway должен быть fail-open

Если:

* Controller unavailable;
* AWG DB unavailable;
* renderer crashed;
* Agent status unavailable;
* временная AWG ошибка;

пользователь не должен потерять VLESS/Xray subscription.

Нужно:

```text
AWG enrichment error
        ↓
return stock Remnawave response
```

Не:

```text
500
```

если stock subscription была доступна.

---

# 25. Сохранение Remnawave response

По возможности сохраняй:

* status code;
* Content-Type;
* subscription-userinfo;
* profile metadata;
* announce;
* HWID behavior;
* response-rule semantics;
* cache-related headers;
* stock entries;
* порядок stock entries, если это важно клиенту.

AWG Gateway должен быть прозрачным слоем.

---

# 26. Capability-aware subscription rendering

Нельзя считать:

```text
формат = capability
```

Например:

```text
CLASH
```

не означает автоматически поддержку AmneziaWG.

И:

```text
SINGBOX
```

не означает автоматически поддержку AWG fork.

Нужно вести capability registry.

Например:

```yaml
incy-ios:
  awgUri: true
  maxAwgVersion: "3.1"

incy-android:
  awgUri: true
  maxAwgVersion: "3.1"

mihomo:
  awgYaml: true
  maxAwgVersion: "..."

legacy-clash:
  awg: false

generic-singbox:
  awg: false

throne:
  awg: true
```

Фактические version constraints должны быть подтверждены тестами и текущими upstream capabilities.

---

# 27. Renderers

Минимально предусмотри архитектуру для:

```text
RawAWGRenderer
Base64Renderer
MihomoRenderer
IncyRenderer
ThroneRenderer
```

Возможно понадобятся дополнительные.

Используй общий interface.

---

# 28. Base64 subscription

Алгоритм:

1. получить stock response Remnawave;
2. определить, действительно ли response Base64-compatible;
3. декодировать;
4. сохранить существующие строки;
5. добавить AWG entries;
6. снова закодировать;
7. сохранить релевантные headers.

Пример plaintext внутри:

```text
vless://...
vless://...
amneziawg://...
```

Не модифицируй существующие VLESS URI без необходимости.

---

# 29. Mihomo

Для Mihomo генерируй только реально поддерживаемый AWG format.

Примерная структура:

```yaml
proxies:
  - name: "Germany | AWG"
    type: wireguard
    server: de.example.com
    port: 51820

    ip: 10.81.0.10/32

    private-key: ...
    public-key: ...

    udp: true
    mtu: 1420

    amnezia-wg-option:
      ...
```

Не предполагай поддержку новой AWG версии только потому, что предыдущая поддерживалась.

Renderer должен учитывать compatibility matrix.

---

# 30. Legacy Clash

Не обещай AWG старому Clash core.

Если клиент не поддерживает нужный extension:

```text
не добавлять AWG entry
```

а не отдавать заведомо нерабочую конфигурацию.

---

# 31. INCY / Throne

INCY и Throne должны иметь отдельные capability definitions.

Не подменяй их generic sing-box или generic xray правилами.

Если клиент поддерживает специальный:

```text
amneziawg://
```

renderer может использовать его.

Если конкретная desktop/mobile версия отличается по capability — учитывай это.

---

# 32. Golden tests для subscription

Subscription rendering должен иметь golden tests.

Для каждого supported client family:

```text
input:
stock Remnawave response
+
AWG peer data
+
client capability/version

expected:
exact enriched output
```

Обязательно тестировать:

* Base64;
* line endings;
* Unicode node names;
* escaping;
* YAML quoting;
* отсутствующий AWG;
* unavailable AWG node;
* incompatible AWG version;
* Controller failure;
* malformed stock response.

---

# 33. AWG traffic

Agent должен уметь получать для каждого peer:

```text
rxBytes
txBytes
latestHandshake
```

Controller должен агрегировать эти данные.

Но AWG traffic не нужно напрямую записывать в Remnawave DB.

---

# 34. Traffic abstractions

Подготовь архитектуру:

```text
TrafficSource
TrafficAggregator
QuotaEvaluator
TrafficSink
```

Сегодня:

```text
AWG Agent → TrafficSource
```

Завтра Remnawave может получить официальный external usage API.

Тогда можно добавить:

```text
RemnawaveNativeTrafficSink
```

без переработки Agent и всей domain model.

---

# 35. Counter correctness

Traffic accounting должен переживать:

* restart Agent;
* restart Controller;
* временно offline node;
* peer recreation;
* counter reset;
* key rotation;
* counter decrease;
* potential rollover;
* reconnect.

Не используй:

```text
currentCounter = lifetimeUsage
```

без обработки reset scenarios.

Храни snapshots/deltas или другую корректную accounting model.

---

# 36. Quota policy

Пока Remnawave не имеет официального внешнего usage injection API, общий quota enforcement может выполняться Controller.

Например:

```text
Remnawave Xray usage
+
AWG usage
=
combined usage
```

При достижении лимита Controller может согласно policy:

* отключить AWG peers;
* через официальный Remnawave API изменить user state;
* уведомить администратора.

Нельзя ради красивой цифры в stock UI напрямую модифицировать traffic columns Remnawave DB.

---

# 37. Standalone Web UI

AWG UI должен быть отдельным приложением.

Концептуальный URL:

```text
/extensions/awg/
```

UI общается только с Controller API.

UI не должен обращаться напрямую:

* к Remnawave DB;
* к AWG DB;
* к Agent management API из браузера.

---

# 38. Что должен показывать UI

Минимально:

## Nodes

* Agent online/offline;
* READY/ERROR;
* version;
* capabilities;
* endpoint;
* server public key;
* applied revision;
* desired revision;
* latest health check.

## Profiles

* protocol version;
* AWG parameters;
* network pools;
* endpoint;
* squads;
* validation;
* revision history.

## Users / Peers

* Remnawave UUID;
* username/display name, если доступно;
* node;
* allocated IP;
* enabled;
* handshake;
* RX/TX.

## Errors

* reconciliation error;
* config validation error;
* Agent apply error;
* IPAM error.

---

# 39. Optional Remnawave frontend link

Допустимо позже сделать минимальный frontend-only patch, который добавляет в меню:

```text
AmneziaWG
```

и ведёт на:

```text
/extensions/awg/
```

Но:

* patch должен быть необязательным;
* business logic в нём запрещена;
* AWG не должен переставать работать после возврата на stock frontend;
* отсутствие пункта меню не должно влиять на Controller/Agent.

Если поддержка этого patch становится дорогой — его можно полностью удалить.

---

# 40. Полное удаление расширения

Должен существовать сценарий:

```text
Stock Remnawave
+
AWG extension
```

→ отключаем Gateway

→ subscription идёт напрямую в Remnawave

→ останавливаем AWG UI

→ останавливаем Controller

→ останавливаем Agents

→ возвращаем stock frontend, если был optional patch

→ Remnawave продолжает работать.

При этом AWG DB может быть сохранена.

После повторного запуска:

```text
Controller
+
Agents
```

reconciliation восстанавливает AWG desired state.

---

# 41. Removal test обязателен

Integration test:

1. запустить stock Remnawave;
2. запустить extension;
3. создать AWG profile;
4. создать peers;
5. проверить subscription;
6. выключить extension;
7. направить subscription напрямую в stock Remnawave;
8. убедиться, что stock Remnawave работает;
9. проверить отсутствие extension-specific migrations в Remnawave DB;
10. снова включить extension;
11. проверить сохранность AWG DB;
12. запустить reconciliation;
13. проверить восстановление peers.

---

# 42. Upgrade compatibility

Вся работа с Remnawave API должна быть локализована в:

```text
packages/remnawave-client
```

или аналогичном adapter layer.

Нельзя размазывать Remnawave DTO/API structures по всей кодовой базе.

Используй interface:

```text
RemnawaveClient
```

и при необходимости version/capability detection.

---

# 43. Upgrade contract tests

Нужно предусмотреть возможность тестировать extension минимум против нескольких поддерживаемых версий Remnawave.

Например:

```text
current stable
previous supported stable
```

Upgrade stock Remnawave не должен автоматически требовать migration AWG database.

AWG DB меняется только когда меняется AWG domain model.

---

# 44. Development environment — критически важное требование

Development machine уже использует VPN.

Этот проект никогда не должен:

* выключать его;
* перенастраивать его;
* обходить его;
* менять его routes;
* менять policy routing;
* менять host DNS;
* менять firewall;
* создавать конфликтующие VPN interfaces.

---

# 45. Docker-only development

Все runtime-компоненты проекта в development запускаются только в Docker containers.

Обязательно в контейнерах запускаются:

* Remnawave Panel;
* Remnawave Node;
* PostgreSQL;
* Redis/Valkey;
* Remnawave subscription-related services;
* AWG Controller;
* AWG Agent;
* AWG test client;
* test targets;
* DNS test service;
* reverse proxies;
* migration tooling;
* protocol testing tools.

Remnawave нельзя устанавливать непосредственно в host OS.

---

# 46. Не устанавливать project dependencies на host

Для целей проекта не выполняй на host:

```text
apt
apt-get
dnf
yum
pacman
zypper
apk
brew
snap
```

Не использовать:

```text
curl ... | sh
```

для установки project dependencies на host.

Не добавлять:

* apt repositories;
* package signing keys;
* system packages;
* language runtimes;

если они нужны только проекту.

Если нужен инструмент:

```text
добавь Dockerfile
```

или:

```text
создай tooling container
```

---

# 47. Что допустимо предполагать на host

Можно предполагать наличие:

* Docker Engine;
* Docker Compose;
* Git;
* Codex environment.

Если чего-то другого нет — не устанавливай автоматически в систему.

---

# 48. Не создавать host services

Запрещено:

* создавать systemd units;
* изменять существующие systemd units;
* создавать cron jobs;
* писать project config в host `/etc`;
* запускать PostgreSQL как host service;
* запускать Redis как host service;
* запускать Remnawave как host process;
* запускать Xray как host process;
* запускать AWG как host process.

Persistent development state должен находиться в Docker volumes или project-owned directories.

---

# 49. Development Remnawave должен быть полностью воспроизводим

Новый разработчик должен иметь возможность:

```text
git clone
docker compose up
```

и получить нужный development environment.

Не должно быть скрытых ручных действий на host.

Не должно быть:

> один раз Codex что-то поставил в `/etc`, поэтому теперь работает.

Если для работы требуется undocumented host configuration — архитектура development environment считается неправильной.

---

# 50. Не модифицировать работающий Remnawave container вручную

Не делать:

```text
docker exec ...
vi ...
cp patched-file ...
npm install ...
```

в stock Remnawave container как часть permanent setup.

Development environment должен быть воспроизводим из:

* Dockerfiles;
* Compose;
* env templates;
* migrations;
* initialization scripts.

---

# 51. Production networking и development networking — разные вещи

Официальная production-инструкция Remnawave может использовать:

```text
network_mode: host
```

Это не означает, что это разрешено на development machine.

Production examples нельзя слепо копировать в development.

Development должен использовать Docker bridge networks и isolated namespaces.

---

# 52. Host networking запрещён в development/test

Запрещено:

```yaml
network_mode: host
```

Запрещено:

```text
--network host
```

для development/network tests.

Также запрещено:

```yaml
privileged: true
pid: host
ipc: host
```

---

# 53. Опасные mounts запрещены

В development/network tests запрещено монтировать host:

```text
/lib/modules
/var/run/netns
/proc
/sys
```

если это открывает возможность менять host networking/kernel state.

Не использовать Docker container как способ фактически получить root control над host network namespace.

---

# 54. Host network namespace нельзя менять

Никогда не выполнять против host namespace:

```text
awg
awg-quick
wg
wg-quick
ip route add
ip route delete
ip route replace
ip rule add
ip rule delete
nft
iptables
modprobe
network-related sysctl changes
```

Эти команды допустимы только внутри явно изолированного test container, если требуются тесту.

---

# 55. Не создавать VPN-интерфейсы на host

Никогда не создавать на development host:

```text
awg0
wg0
tun0
test-vpn
```

или аналогичные project interfaces.

TUN/AWG/WG существуют только внутри контейнерных namespaces.

---

# 56. Userspace AmneziaWG для development

Для development и CI предпочитай:

```text
amneziawg-go
```

или другой userspace AWG backend.

Не требуй установки kernel AmneziaWG module на development host.

Не монтируй `/lib/modules`.

---

# 57. Разрешённые capabilities test container

AWG test client/server при необходимости могут получить минимальные capabilities:

```text
NET_ADMIN
```

и если действительно нужен ping:

```text
NET_RAW
```

Допустимо:

```text
/dev/net/tun
```

Не использовать `privileged: true` только ради удобства.

---

# 58. Full-tunnel тестируется только внутри container

Разрешено тестировать:

```text
AllowedIPs = 0.0.0.0/0
AllowedIPs = ::/0
```

только внутри isolated test-client container.

Внутри этого контейнера допускается:

```text
default route → AWG
```

На development host — никогда.

---

# 59. Ожидаемая topology

Безопасный вариант:

```text
Development host
│
├── existing host VPN
│
├── Docker Engine
│
└── Docker bridge
      │
      ├── AWG client container
      │       ├── eth0
      │       └── awg0
      │
      ├── AWG server container
      │
      └── target container
```

Full tunnel меняет маршруты только:

```text
AWG client namespace
```

а не:

```text
host namespace
```

---

# 60. VPN поверх VPN допустим

Если development host уже сам находится за VPN:

```text
Docker client
   ↓
host VPN
   ↓
remote AWG endpoint
```

это допустимо для smoke test.

Не пытайся ради теста обходить host VPN routing.

Если host VPN блокирует нужный UDP:

```text
не изменяй host
```

Перенеси тест в:

* disposable VM;
* dedicated CI runner;
* test VPS.

---

# 61. Hermetic Docker network lab

Обязателен fully isolated test topology.

Например:

```text
AWG test client
       ↓
Docker synthetic underlay
       ↓
AWG test server
       ↓
test target
```

Он не должен требовать публичный Internet endpoint для базовых protocol tests.

---

# 62. Hermetic tests должны проверять

Минимум:

* handshake;
* TCP через tunnel;
* UDP через tunnel;
* DNS, если предусмотрен;
* full tunnel;
* split tunnel;
* peer creation;
* peer disable;
* peer enable;
* peer delete;
* reconnect;
* key rotation;
* server restart;
* Agent restart;
* Controller restart;
* reconciliation;
* traffic counters;
* rollback после плохой revision.

---

# 63. Remote smoke tests

Могут существовать отдельно.

Но client всё равно должен работать внутри Docker container.

Если remote test небезопасен на developer machine:

```text
не запускать
```

а пометить:

```text
requires isolated runner
```

---

# 64. Автоматический network safety lint

Создай скрипт вроде:

```text
scripts/check-network-test-safety
```

Он должен проверять Compose/test manifests.

Test suite должна падать при обнаружении в development/test:

```text
network_mode: host
privileged: true
pid: host
ipc: host
/lib/modules
/var/run/netns
опасных host /proc mounts
опасных host /sys mounts
```

---

# 65. Host integrity test

Перед network E2E зафиксировать минимум:

```text
host default routes
host policy rules
relevant host interfaces
existing VPN interface
```

После теста сравнить.

Docker bridge/veth interfaces могут появляться — это нормально.

Но тест должен падать, если:

* изменился host default route;
* изменились host policy rules без ожидаемой Docker-причины;
* появился AWG/WG/TUN project interface на host;
* исчез existing VPN;
* был изменён host firewall project-specific образом.

---

# 66. Development compose и production compose разделить

Используй разные manifests.

Например:

```text
deploy/dev/
deploy/production/
```

Development manifests подчиняются самым строгим host-safety правилам.

Production manifests могут иметь другую topology, но никогда не должны автоматически запускаться Codex на development host.

---

# 67. Agent и Remnawave Node в development

Даже если production Remnawave Node обычно использует host networking, development test environment должен по возможности моделировать его через Docker bridge.

Если конкретную production-specific функцию нельзя воспроизвести без host network:

* не ломай development host;
* вынеси test в disposable environment;
* документируй limitation.

---

# 68. Secrets

Никогда не commit:

* Remnawave production tokens;
* Agent credentials;
* client private keys;
* server private keys;
* mTLS private keys;
* real production config.

Используй:

* `.env.example`;
* Docker secrets;
* injected env vars;
* generated test credentials.

Fixtures должны использовать только fake/test secrets.

---

# 69. Logs

Structured logs должны иметь correlation IDs.

Но никогда не логировать:

```text
client private key
server private key
Remnawave token
mTLS private key
full subscription containing private AWG key
```

При необходимости redaction должен происходить до logger.

---

# 70. Observability

Минимальные Prometheus metrics:

```text
awg_nodes_total
awg_nodes_ready
awg_peers_total

awg_peer_rx_bytes
awg_peer_tx_bytes
awg_peer_latest_handshake_seconds

awg_reconcile_runs_total
awg_reconcile_errors_total

awg_apply_total
awg_apply_errors_total

awg_subscription_render_total
awg_subscription_render_errors_total
```

Не помещай sensitive high-cardinality values в labels.

---

# 71. Health

Разделяй:

```text
liveness
readiness
dependency health
```

Controller может быть alive, даже если Remnawave временно недоступен.

Gateway может быть ready в fail-open mode, если stock Remnawave доступен, даже если AWG Controller временно unavailable.

---

# 72. Failure scenarios

Обязательно тестировать:

## Controller offline

Existing AWG tunnel должен продолжать работать.

Agent не должен удалить всё только потому, что Controller временно недоступен.

## Agent offline

Controller хранит desired state.

После возвращения Agent выполняется reconciliation.

## Remnawave offline

Controller не должен мгновенно удалить всех пользователей.

Нужна conservative policy.

## Gateway AWG failure

Возвращается stock Remnawave subscription.

## Bad AWG revision

Rollback.

## DB restart

Нет потери persistent state.

---

# 73. Никаких destructive actions при неопределённости

Например:

```text
Remnawave API timeout
```

не означает:

```text
все пользователи удалены
```

Нужно различать:

```text
authoritative empty state
```

и:

```text
unable to fetch state
```

---

# 74. Concurrency

Reconciliation должен безопасно работать при нескольких событиях одновременно.

Тестировать:

* webhook + periodic reconcile;
* duplicate webhook;
* два concurrent user creates;
* concurrent IP allocation;
* disable во время Agent reconnect;
* profile update во время user creation.

---

# 75. Idempotency

API операций Controller→Agent должны поддерживать повторный вызов.

Например:

```text
ensurePeer(...)
```

лучше, чем:

```text
createPeerExactlyOnce(...)
```

когда это возможно.

Retry network request не должен создавать duplicate peer.

---

# 76. Versioning contracts

Controller и Agent не должны требовать абсолютно одинаковой версии binary для каждого rollout.

Протокол должен позволять проверить:

```text
Agent protocol version
Controller protocol version
capabilities
```

и вернуть понятную incompatible ошибку.

---

# 77. Migration policy

AWG database migrations:

* должны быть versioned;
* backward-aware;
* иметь backup guidance;
* не должны затрагивать Remnawave DB.

Перед destructive migration должна существовать понятная strategy.

---

# 78. Backup / Restore

Документируй:

* какие Docker volumes являются persistent;
* как backup AWG PostgreSQL;
* как backup Agent server keys;
* как восстановить Controller;
* как восстановить Agent без смены server identity;
* что произойдёт, если Agent key потерян.

---

# 79. Не удалять данные пользователей при uninstall

Удаление контейнеров приложения и отключение integration не должно автоматически уничтожать:

```text
AWG DB volume
Agent server keys
```

Persistent data deletion должна быть отдельным явным действием.

---

# 80. Testing pyramid

Используй:

## Unit

* IPAM;
* state calculation;
* validators;
* capability matching;
* traffic deltas;
* renderer primitives.

## Contract

* Controller ↔ Agent;
* Controller ↔ Remnawave adapter.

## Golden

* subscriptions/configs.

## Integration

* PostgreSQL;
* Controller;
* mock/real Agent;
* stock Remnawave containers.

## Network E2E

* real userspace AWG tunnel в Docker namespace.

---

# 81. Не считать config generation доказательством работы

Тест:

```text
AWG config generated
```

недостаточен.

Должен существовать хотя бы один тест:

```text
peer handshake established
```

и:

```text
real payload traversed tunnel
```

---

# 82. Процесс реализации

Перед крупной реализацией:

1. изучить существующий repository;
2. зафиксировать архитектуру;
3. определить component boundaries;
4. определить contracts;
5. определить data ownership;
6. построить Docker development lab;
7. только после этого писать protocol/networking integration.

---

# 83. Порядок при cross-component изменениях

Если feature требует Controller + Agent + Gateway:

сначала:

```text
contract/schema
```

потом параллельно:

```text
Controller implementation
Agent implementation
Gateway implementation
```

Не позволяй нескольким агентам независимо придумывать несовместимые payload formats.

---

# 84. Git hygiene

Изменения должны быть небольшими и тематическими.

Не смешивать:

```text
massive formatting
+
architecture rewrite
+
feature
```

в одном изменении без необходимости.

Не изменять unrelated files.

---

# 85. Documentation

После существенной архитектурной реализации обновить:

```text
docs/architecture.md
```

Для важных решений создавать ADR:

```text
docs/decisions/XXXX-*.md
```

Например:

```text
why separate AWG DB
why fail-open subscriptions
why userspace AWG in development
why no host networking
```

---

# 86. Reviewer обязателен для sensitive changes

После изменений в:

* network isolation;
* secrets;
* Controller↔Agent auth;
* subscription proxying;
* Remnawave integration;
* traffic accounting;
* database migrations;

нужно запускать независимого reviewer.

Reviewer не должен автоматически доверять реализации.

---

# 87. Definition of Done

Проект/существенная milestone не считается завершённой, пока не выполнено следующее:

* stock Remnawave не модифицирован;
* Remnawave DB не содержит AWG migrations;
* Remnawave в development работает только в Docker;
* project dependencies не установлены на host;
* host VPN не затронут;
* host routes не затронуты;
* AWG E2E работает внутри Docker namespace;
* Controller умеет работать с Remnawave users;
* squads влияют на desired state;
* AWG Profile проходит validation;
* Agent сообщает capabilities;
* revision применима;
* broken revision rollback'ится;
* Node получает READY;
* пользователь автоматически получает peer;
* disabled пользователь теряет AWG access;
* enabled пользователь восстанавливает access;
* deleted пользователь удаляется с нод;
* Base64 renderer покрыт golden tests;
* Mihomo renderer покрыт golden tests;
* incompatible client не получает AWG;
* Gateway fail-open работает;
* stock subscription не ломается при AWG outage;
* traffic counters собираются;
* duplicate IP allocation невозможен;
* extension removal test проходит;
* после повторного запуска reconciliation восстанавливает AWG;
* reviewer не обнаруживает critical нарушений.

---

# 88. Приоритеты при архитектурных решениях

Если есть несколько вариантов, используй следующий порядок приоритетов:

1. безопасность development host;
2. независимость от stock Remnawave internals;
3. обратимость;
4. upgrade compatibility;
5. сохранность пользовательских данных;
6. fail-open для существующих Remnawave функций;
7. идемпотентность;
8. testability;
9. observability;
10. удобство администратора;
11. производительность;
12. удобство реализации.

---

# 89. Если требование конфликтует с удобством

Если проще:

```text
пропатчить Remnawave backend
```

но можно сделать отдельно:

```text
Controller + API
```

выбирай отдельный Controller.

Если проще:

```text
network_mode: host на development machine
```

но можно использовать isolated Docker lab:

выбирай isolated Docker lab.

Если реальный test невозможно безопасно выполнить:

не ломай host ради теста.

Перенеси его на isolated runner.

---

# 90. Финальный принцип

Целевая архитектура должна позволять администратору воспринимать AWG почти как штатный протокол Remnawave:

```text
создал/выбрал ноду
→ написал конфиг
→ validation
→ Apply
→ пользователи получили peers
→ подписки обновились
```

Но внутренне система должна оставаться независимым extension:

```text
Remnawave
    │
public API/webhooks
    │
AWG Controller
    │
AWG Agents
    │
AmneziaWG
```

Так, чтобы в любой момент можно было сказать:

```text
AWG больше не нужен
```

отключить extension и остаться с полностью stock Remnawave без миграции пользователей, без отката базы данных и без восстановления пропатченных компонентов.

Это является главным архитектурным критерием проекта.
