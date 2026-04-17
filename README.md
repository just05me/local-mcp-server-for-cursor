# local MCP server for Cursor (Obsidian “Second Brain”)

Локальный MCP-сервер (stdio) для Cursor, который даёт агенту доступ к вашему Obsidian vault (markdown‑файлы) через инструменты: заметки/поиск/задачи/ревью и пишет лог каждого вызова прямо в vault.

Источник архитектуры (адаптировано под Cursor): `https://habr.com/ru/companies/bothub/articles/985736/`

## Что это даёт

- **Работа строго локально**: vault остаётся у вас на диске.
- **Инструменты для агента**: CRUD по markdown, поиск, задачи (Obsidian Tasks‑стиль), ежедневники/ревью.
- **Автологирование**: каждое обращение агента сохраняется в `Cursor Logs/` внутри vault.

## Требования

- Python **3.11+**
- Cursor с поддержкой **MCP (stdio)**

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Быстрый запуск (проверка руками)

```bash
.venv/bin/python obsidian-second-brain-mcp.py --vault "/ABS/PATH/TO/YOUR/OBSIDIAN_VAULT"
```

## Подключение к Cursor

В проекте рядом с кодом создайте файл `.cursor/mcp.json` на основе шаблона `.cursor/mcp.example.json` и укажите путь к вашему vault.

Пример (минимально):

```json
{
  "mcpServers": {
    "Obsidian Second Brain": {
      "command": "bash",
      "args": [
        "-lc",
        "cd /ABS/PATH/TO/THIS/REPO && source .venv/bin/activate && python obsidian-second-brain-mcp.py --vault \"/ABS/PATH/TO/YOUR/OBSIDIAN_VAULT\""
      ]
    }
  }
}
```

После этого перезапустите Cursor (или пересканируйте MCP), и инструменты сервера станут доступны агенту.

## Рекомендуемая структура vault

```
Vault/
├── Daily Journal/
├── Weekly Reviews/
├── tasks.md
├── Cursor Logs/
├── Templates/
└── Inbox/
```

## Логи

- **Лог инструментов**: `Vault/Cursor Logs/cursor-actions-YYYY-MM-DD.md`
- **Отчёты чатов**: `Vault/Cursor Logs/chat-report-YYYY-MM-DD-HHMMSS.md`

## Самопроверка (необязательно)

```bash
.venv/bin/python obsidian-second-brain-mcp.py --vault /tmp/obs-vault --self-test
```
