# Codex setup

Этот пакет содержит project-scoped конфигурацию Codex для проекта Remnawave + AmneziaWG.

После распаковки в корень проекта:

1. Сохрани мастер-промпт из чата в:
   `docs/MASTER_PROMPT.md`

2. Убедись, что в корне есть:
   - `AGENTS.md`
   - `.codex/config.toml`
   - `.codex/agents/*.toml`

3. Запускай Codex из корня репозитория.

Рекомендуемый первый запрос:

Read AGENTS.md and docs/MASTER_PROMPT.md first.

Use the project custom subagents.

Have architect inspect the requirements and existing repository first.
Then delegate independent implementation work to the appropriate component agents.
Use network_qa to build and validate the Docker-only development lab before performing real AWG connectivity tests.
After implementation, run reviewer independently.

Do not install anything project-specific on the host and do not modify host networking or the existing VPN.

Отвечай мне на русском языке.
