#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional

from mcp.server.fastmcp import FastMCP


Priority = Literal["high", "medium", "low", "none"]


class MCPError(Exception):
    def __init__(self, code: str, message: str, details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def ok(result: Any, warnings: Optional[list[str]] = None) -> dict[str, Any]:
    return {"ok": True, "result": result, "warnings": warnings or []}


def err(code: str, message: str, details: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "details": details or {}}}


RU_WEEKDAYS = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}

RU_MONTHS_GENITIVE = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

RU_WEEKDAY_ALIASES = {
    "понедельник": 0,
    "пн": 0,
    "вторник": 1,
    "вт": 1,
    "среда": 2,
    "ср": 2,
    "четверг": 3,
    "чт": 3,
    "пятница": 4,
    "пт": 4,
    "суббота": 5,
    "сб": 5,
    "воскресенье": 6,
    "вс": 6,
}


DEFAULT_DAILY_TEMPLATE = """# {{YYYY-MM-DD dddd}}

## Фокус на сегодня
- 

## Задачи
```tasks
not done
due before {{today}}
```

## Привычки
- [ ] 🚶 Пройти 10к шагов
- [ ] 🍽️ Питание в норме
- [ ] 😴 Сон 7+ часов
- [ ] 💻 1 час работы над проектом

## Рефлексия
### Что сделано хорошо?

### Что можно улучшить?

### Мысли дня
"""


def _today_local() -> dt.date:
    return dt.datetime.now().astimezone().date()


def _now_local_time_str() -> str:
    return dt.datetime.now().astimezone().strftime("%H:%M:%S")


def _now_local_compact() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")


def _iso_date(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def _ensure_utf8_text(s: str) -> str:
    # На всякий случай нормализуем переводы строк.
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_tags(tags: Optional[list[str]]) -> list[str]:
    if not tags:
        return []
    norm: list[str] = []
    for t in tags:
        t = (t or "").strip()
        if not t:
            continue
        if t.startswith("#"):
            t = t[1:]
        # Очень мягкая нормализация: пробелы -> дефис
        t = re.sub(r"\s+", "-", t)
        norm.append(t)
    # unique, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in norm:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


def _priority_to_emoji(p: Priority) -> str:
    return {"high": "⏫", "medium": "🔼", "low": "🔽", "none": ""}.get(p, "")


def _parse_iso_date(s: Optional[str]) -> Optional[dt.date]:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s)
    except Exception as e:  # noqa: BLE001
        raise MCPError("INVALID_ARGUMENT", f"Неверный формат даты: {s}. Ожидается YYYY-MM-DD.", {"value": s}) from e


@dataclass(frozen=True)
class VaultPaths:
    vault: Path

    @property
    def daily_journal_dir(self) -> Path:
        return self.vault / "Daily Journal"

    @property
    def weekly_reviews_dir(self) -> Path:
        return self.vault / "Weekly Reviews"

    @property
    def templates_dir(self) -> Path:
        return self.vault / "Templates"

    @property
    def cursor_logs_dir(self) -> Path:
        return self.vault / "Cursor Logs"

    @property
    def tasks_file(self) -> Path:
        return self.vault / "tasks.md"


class VaultFS:
    def __init__(self, vault_root: str):
        self.vault = Path(vault_root).expanduser().resolve()
        if not self.vault.exists():
            try:
                self.vault.mkdir(parents=True, exist_ok=True)
            except PermissionError as e:
                raise MCPError(
                    "INVALID_ARGUMENT",
                    "Нет прав на создание/доступ к папке vault. Укажи существующий путь или папку в домашней директории.",
                    {"vault": vault_root},
                ) from e
            except FileNotFoundError as e:
                raise MCPError(
                    "INVALID_ARGUMENT",
                    "Некорректный путь к vault (часть пути не существует). Укажи реальный абсолютный путь.",
                    {"vault": vault_root},
                ) from e
        if not self.vault.is_dir():
            raise MCPError("INVALID_ARGUMENT", "Параметр --vault должен указывать на папку.", {"vault": vault_root})

        self.paths = VaultPaths(self.vault)

    def safe_path(self, relative: str) -> Path:
        rel = (relative or "").strip().replace("\\", "/")
        if not rel:
            raise MCPError("INVALID_ARGUMENT", "Путь не может быть пустым.", {"relative": relative})
        if rel.startswith("/") or rel.startswith("~"):
            raise MCPError("OUTSIDE_VAULT", "Разрешены только относительные пути внутри vault.", {"relative": relative})
        if any(part in ("..", "") for part in rel.split("/")):
            raise MCPError("OUTSIDE_VAULT", "Запрещённые сегменты пути (..).", {"relative": relative})

        p = (self.vault / rel).resolve()
        try:
            p.relative_to(self.vault)
        except Exception as e:  # noqa: BLE001
            raise MCPError("OUTSIDE_VAULT", "Путь выходит за пределы vault.", {"relative": relative}) from e
        return p

    def ensure_parent_dir(self, p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)

    def read_text(self, relative: str) -> str:
        p = self.safe_path(relative)
        if not p.exists():
            raise MCPError("NOT_FOUND", "Файл не найден.", {"file_name": relative})
        if not p.is_file():
            raise MCPError("INVALID_ARGUMENT", "Ожидался файл.", {"file_name": relative})
        try:
            return p.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            raise MCPError("IO_ERROR", "Не удалось прочитать файл.", {"file_name": relative}) from e

    def write_text(self, relative: str, content: str) -> tuple[int, bool]:
        p = self.safe_path(relative)
        self.ensure_parent_dir(p)
        created = not p.exists()
        try:
            data = _ensure_utf8_text(content)
            p.write_text(data, encoding="utf-8")
            return (len(data.encode("utf-8")), created)
        except Exception as e:  # noqa: BLE001
            raise MCPError("IO_ERROR", "Не удалось записать файл.", {"file_name": relative}) from e

    def append_text(self, relative: str, content: str) -> tuple[int, bool]:
        p = self.safe_path(relative)
        self.ensure_parent_dir(p)
        created = not p.exists()
        try:
            data = _ensure_utf8_text(content)
            if p.exists():
                existing = p.read_text(encoding="utf-8")
                if existing and not data.startswith("\n"):
                    data = "\n" + data
                p.write_text(existing + data, encoding="utf-8")
                appended_bytes = len(data.encode("utf-8"))
            else:
                p.write_text(data, encoding="utf-8")
                appended_bytes = len(data.encode("utf-8"))
            return (appended_bytes, created)
        except Exception as e:  # noqa: BLE001
            raise MCPError("IO_ERROR", "Не удалось добавить текст в файл.", {"file_name": relative}) from e

    def list_md_files(self, folder: Optional[str]) -> list[str]:
        root = self.vault if not folder else self.safe_path(folder)
        if not root.exists():
            raise MCPError("NOT_FOUND", "Папка не найдена.", {"folder": folder})
        if not root.is_dir():
            raise MCPError("INVALID_ARGUMENT", "Ожидалась папка.", {"folder": folder})
        out: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.lower().endswith(".md"):
                    p = (Path(dirpath) / fn).resolve()
                    try:
                        rel = p.relative_to(self.vault).as_posix()
                    except Exception:
                        continue
                    out.append(rel)
        out.sort()
        return out


def _actions_log_file(vaultfs: VaultFS, day: dt.date) -> Path:
    vaultfs.paths.cursor_logs_dir.mkdir(parents=True, exist_ok=True)
    return vaultfs.paths.cursor_logs_dir / f"cursor-actions-{_iso_date(day)}.md"


def _append_actions_log(vaultfs: VaultFS, tool_name: str, params: Any, status: str, result_summary: str) -> str:
    day = _today_local()
    p = _actions_log_file(vaultfs, day)
    ts = _now_local_time_str()

    def _params_md(x: Any) -> str:
        try:
            s = json.dumps(x, ensure_ascii=False, sort_keys=True)
        except Exception:
            s = str(x)
        # слегка ограничим объём
        if len(s) > 4000:
            s = s[:4000] + "…(truncated)"
        return s

    entry = (
        f"## {ts} | {tool_name}\n"
        f"**Параметры:** `{_params_md(params)}`  \n"
        f"**Результат:** {result_summary}  \n"
        f"**Статус:** {status}\n\n---\n"
    )
    try:
        p.write_text(p.read_text(encoding="utf-8") + entry if p.exists() else entry, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        # Логирование не должно ломать основной tool.
        _ = e
    return p.relative_to(vaultfs.vault).as_posix()


def _tool_autolog(
    vaultfs: VaultFS,
    tool_name: str,
    params: Any,
    fn: Callable[..., dict[str, Any]],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        res = fn(*args, **kwargs)
        if isinstance(res, dict) and res.get("ok") is True:
            _append_actions_log(vaultfs, tool_name, params, "✅ успех", _summarize_result(res.get("result")))
        else:
            _append_actions_log(vaultfs, tool_name, params, "❌ ошибка", _summarize_result(res))
        return res
    except MCPError as e:
        payload = err(e.code, e.message, e.details)
        _append_actions_log(vaultfs, tool_name, params, "❌ ошибка", f"`{e.code}`: {e.message}")
        return payload
    except Exception as e:  # noqa: BLE001
        payload = err("INTERNAL_ERROR", "Внутренняя ошибка сервера (инструмент не выполнился).", {"exception": str(e)})
        _append_actions_log(vaultfs, tool_name, params, "❌ ошибка", f"`INTERNAL_ERROR`: {str(e)}")
        return payload


def _summarize_result(x: Any) -> str:
    if x is None:
        return "—"
    if isinstance(x, str):
        s = x.strip()
        return s if len(s) <= 200 else s[:200] + "…"
    if isinstance(x, dict):
        keys = ", ".join(list(x.keys())[:8])
        return f"объект ({keys})"
    if isinstance(x, list):
        return f"список (len={len(x)})"
    return str(x)


def _ensure_tasks_file(vaultfs: VaultFS) -> None:
    if not vaultfs.paths.tasks_file.exists():
        vaultfs.paths.tasks_file.write_text("# Tasks\n\n## Inbox\n", encoding="utf-8")
        return
    content = vaultfs.paths.tasks_file.read_text(encoding="utf-8")
    if re.search(r"^##\s+Inbox\s*$", content, flags=re.MULTILINE) is None:
        vaultfs.paths.tasks_file.write_text(content.rstrip() + "\n\n## Inbox\n", encoding="utf-8")


def _insert_under_heading(md: str, heading: str, block: str) -> tuple[str, str]:
    """
    Вставляет block сразу после строки заголовка '## heading'.
    Возвращает (new_md, inserted_under).
    """
    lines = md.splitlines()
    pat = re.compile(rf"^##\s+{re.escape(heading)}\s*$")
    for i, line in enumerate(lines):
        if pat.match(line):
            insert_at = i + 1
            # Пропустим одну пустую строку после заголовка, если она есть
            if insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            new_lines = lines[:insert_at] + [block] + lines[insert_at:]
            return ("\n".join(new_lines).rstrip() + "\n", heading)
    # Если заголовка нет — добавим в конец
    md2 = md.rstrip() + f"\n\n## {heading}\n{block}\n"
    return (md2, heading)


def _render_daily_template(template: str, day: dt.date) -> str:
    weekday = RU_WEEKDAYS[day.weekday()]
    header = f"{_iso_date(day)} {weekday}"
    out = template
    out = out.replace("{{YYYY-MM-DD dddd}}", header)
    out = out.replace("{{today}}", _iso_date(day))
    return _ensure_utf8_text(out).rstrip() + "\n"


TASK_OPEN_RE = re.compile(r"^\s*-\s*\[\s*\]\s+(.*)$")
TASK_DONE_RE = re.compile(r"^\s*-\s*\[\s*x\s*\]\s+(.*)$", flags=re.IGNORECASE)
DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
ID_RE = re.compile(r"<!--\s*id:(t_[a-z0-9]+)\s*-->")


def _extract_tags_from_text(task_text: str) -> list[str]:
    # Разрешим кириллицу/латиницу/цифры/дефис/подчёркивание.
    tags = re.findall(r"#([\w\u0400-\u04FF-]+)", task_text)
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


def _extract_priority_from_text(task_text: str) -> Priority:
    if "⏫" in task_text:
        return "high"
    if "🔼" in task_text:
        return "medium"
    if "🔽" in task_text:
        return "low"
    return "none"


def _parse_due_from_text(task_text: str) -> Optional[str]:
    m = DUE_RE.search(task_text)
    return m.group(1) if m else None


def _make_task_id() -> str:
    return "t_" + secrets.token_hex(4)


def _build_task_line(description: str, due_date: Optional[str], tags: list[str], priority: Priority) -> tuple[str, str]:
    description = (description or "").strip()
    if not description:
        raise MCPError("INVALID_ARGUMENT", "description не может быть пустым.", {})

    parts: list[str] = [f"- [ ] {description}"]
    if due_date:
        _parse_iso_date(due_date)  # validate
        parts.append(f"📅 {due_date}")

    for t in _normalize_tags(tags):
        parts.append(f"#{t}")

    pe = _priority_to_emoji(priority)
    if pe:
        parts.append(pe)

    task_id = _make_task_id()
    parts.append(f"<!-- id:{task_id} -->")
    return (" ".join(parts), task_id)


def _iter_md_files_for_search(vaultfs: VaultFS, folder: Optional[str]) -> Iterable[Path]:
    if folder:
        root = vaultfs.safe_path(folder)
    else:
        root = vaultfs.vault
    excludes = set()
    if not folder:
        excludes = {"Cursor Logs", "Templates"}

    for dirpath, dirnames, filenames in os.walk(root):
        # exclude dirs
        if excludes:
            dirnames[:] = [d for d in dirnames if d not in excludes]
        for fn in filenames:
            if fn.lower().endswith(".md"):
                yield (Path(dirpath) / fn)


def _search_notes_impl(vaultfs: VaultFS, query: str, folder: Optional[str]) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        raise MCPError("INVALID_ARGUMENT", "query не может быть пустым.", {})

    q_lower = q.lower()
    hits: list[dict[str, Any]] = []
    for p in _iter_md_files_for_search(vaultfs, folder):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            if q_lower in line.lower():
                excerpt = line.strip()
                if len(excerpt) > 240:
                    excerpt = excerpt[:240] + "…"
                try:
                    rel = p.resolve().relative_to(vaultfs.vault).as_posix()
                except Exception:
                    rel = p.name
                hits.append({"file_name": rel, "line": idx, "excerpt": excerpt})
                if len(hits) >= 2000:
                    return {"query": q, "scope_folder": folder or "<vault>", "hits": hits}
    return {"query": q, "scope_folder": folder or "<vault>", "hits": hits}


def _get_open_tasks_impl(vaultfs: VaultFS, due_before: Optional[str], tags: Optional[list[str]]) -> dict[str, Any]:
    due_before_d = _parse_iso_date(due_before) if due_before else None
    wanted_tags = set(_normalize_tags(tags))
    tasks: list[dict[str, Any]] = []

    for p in _iter_md_files_for_search(vaultfs, folder=None):
        # дополнительные исключения (уже сделаны в _iter... по умолчанию)
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = p.resolve().relative_to(vaultfs.vault).as_posix()
        for idx, line in enumerate(text.splitlines(), start=1):
            m = TASK_OPEN_RE.match(line)
            if not m:
                continue
            raw = line.strip()
            due = _parse_due_from_text(raw)
            if due and due_before_d:
                try:
                    due_d = dt.date.fromisoformat(due)
                except Exception:
                    due_d = None
                if due_d and due_d >= due_before_d:
                    continue
            line_tags = _extract_tags_from_text(raw)
            if wanted_tags:
                if not (wanted_tags.intersection(set(line_tags))):
                    continue
            tasks.append(
                {
                    "file_name": rel,
                    "line": idx,
                    "text": raw,
                    "due_date": due,
                    "tags": line_tags,
                    "priority": _extract_priority_from_text(raw),
                }
            )
    return {"count": len(tasks), "tasks": tasks}


def _iso_week_file_name(week_start: dt.date) -> str:
    iso_year, iso_week, _ = week_start.isocalendar()
    return f"{iso_year}-W{iso_week:02d}.md"


def _week_start_from_any_date(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _generate_weekly_review_impl(vaultfs: VaultFS, week_start: Optional[str]) -> dict[str, Any]:
    if week_start:
        ws = _parse_iso_date(week_start)
        if ws is None:
            raise MCPError("INVALID_ARGUMENT", "week_start не может быть пустой строкой.", {})
        ws = _week_start_from_any_date(ws)
    else:
        ws = _week_start_from_any_date(_today_local())

    we = ws + dt.timedelta(days=6)
    vaultfs.paths.weekly_reviews_dir.mkdir(parents=True, exist_ok=True)
    out_rel = f"Weekly Reviews/{_iso_week_file_name(ws)}"
    out_path = vaultfs.safe_path(out_rel)

    completed: list[str] = []
    daily_found = 0
    for i in range(7):
        day = ws + dt.timedelta(days=i)
        rel = f"Daily Journal/{_iso_date(day)}.md"
        p = vaultfs.safe_path(rel)
        if not p.exists():
            continue
        daily_found += 1
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for line in text.splitlines():
            if TASK_DONE_RE.match(line):
                completed.append(line.strip())

    open_tasks = _get_open_tasks_impl(vaultfs, due_before=None, tags=None)["tasks"]

    content_lines: list[str] = []
    content_lines.append(f"# Weekly Review {ws.isocalendar().year}-W{ws.isocalendar().week:02d}")
    content_lines.append("")
    content_lines.append(f"**Период:** {_iso_date(ws)} — {_iso_date(we)}")
    content_lines.append("")
    content_lines.append("## Итоги")
    content_lines.append("- ")
    content_lines.append("")
    content_lines.append("## Выполненные задачи")
    if completed:
        content_lines.extend(completed)
    else:
        content_lines.append("- (нет найденных выполненных задач в Daily Journal)")
    content_lines.append("")
    content_lines.append("## Заблокированные проекты")
    content_lines.append("- ")
    content_lines.append("")
    content_lines.append("## Приоритеты на следующую неделю")
    content_lines.append("- ")
    content_lines.append("")
    content_lines.append("## Незакрытые задачи (carry-over)")
    if open_tasks:
        for t in open_tasks[:200]:
            content_lines.append(f"- {t['text']} ({t['file_name']}:{t['line']})")
        if len(open_tasks) > 200:
            content_lines.append(f"- … ещё {len(open_tasks) - 200}")
    else:
        content_lines.append("- (нет открытых задач)")
    content_lines.append("")

    final = "\n".join(content_lines).rstrip() + "\n"
    bytes_written, created = vaultfs.write_text(out_rel, final)
    _ = bytes_written
    return {
        "file_name": out_rel,
        "week_start": _iso_date(ws),
        "week_end": _iso_date(we),
        "daily_notes_found": daily_found,
        "open_tasks_carried": len(open_tasks),
        "completed_tasks_found": len(completed),
        "created": created,
    }


def _parse_ru_date_from_text(text: str, today: dt.date) -> Optional[dt.date]:
    s = text.lower()

    if "сегодня" in s:
        return today
    if "завтра" in s:
        return today + dt.timedelta(days=1)
    if "послезавтра" in s:
        return today + dt.timedelta(days=2)

    if "до конца месяца" in s:
        # последний день месяца
        first_next = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        return first_next - dt.timedelta(days=1)

    if "на следующей неделе" in s:
        ws = _week_start_from_any_date(today) + dt.timedelta(days=7)
        return ws

    # "в пятницу", "к пятнице", "до пятницы"
    m = re.search(r"\b(?:в|к|до)\s+([а-яё]{2,12})\b", s)
    if m:
        wd_raw = m.group(1)
        wd = RU_WEEKDAY_ALIASES.get(wd_raw)
        if wd is not None:
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + dt.timedelta(days=delta)

    # "20 апреля"
    m2 = re.search(r"\b(\d{1,2})\s+([а-яё]+)\b", s)
    if m2:
        day = int(m2.group(1))
        month_name = m2.group(2)
        month = RU_MONTHS_GENITIVE.get(month_name)
        if month:
            year = today.year
            try:
                candidate = dt.date(year, month, day)
            except Exception:
                return None
            # если дата уже прошла "сильно", можно трактовать как следующий год
            if candidate < today - dt.timedelta(days=7):
                try:
                    candidate2 = dt.date(year + 1, month, day)
                    return candidate2
                except Exception:
                    return candidate
            return candidate

    # ISO date "2026-04-20"
    m3 = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", s)
    if m3:
        try:
            return dt.date.fromisoformat(m3.group(1))
        except Exception:
            return None

    return None


def _extract_tasks_from_note_impl(vaultfs: VaultFS, file_name: str) -> dict[str, Any]:
    src = vaultfs.read_text(file_name)
    today = _today_local()

    candidates: list[str] = []
    markers = ("нужно", "надо", "сделать", "договорились", "задача", "todo", "срочно")
    for raw_line in src.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        ll = line.lower()
        if any(m in ll for m in markers):
            # уберём маркерные префиксы
            line2 = re.sub(r"^(?:[-*]\s*)?(?:задача:|todo:)\s*", "", line, flags=re.IGNORECASE).strip()
            candidates.append(line2)

    extracted: list[dict[str, Any]] = []
    for c in candidates[:100]:
        due_d = _parse_ru_date_from_text(c, today)
        due = _iso_date(due_d) if due_d else None
        tags: list[str] = []
        if due is None:
            tags.append("needs-date")
        # мягко попробуем вытащить #теги прямо из текста
        tags.extend(_extract_tags_from_text(c))
        description = re.sub(r"#([\w\u0400-\u04FF-]+)", "", c).strip()
        description = re.sub(r"\s+", " ", description).strip(" -")
        if not description:
            continue
        extracted.append({"description": description, "due_date": due, "tags": _normalize_tags(tags)})

    _ensure_tasks_file(vaultfs)
    tasks_md = vaultfs.paths.tasks_file.read_text(encoding="utf-8")
    inserted_lines: list[str] = []
    for t in extracted:
        line, _tid = _build_task_line(t["description"], t["due_date"], t["tags"], "none")
        inserted_lines.append(line)

    if inserted_lines:
        block = "\n".join(inserted_lines)
        new_md, _ = _insert_under_heading(tasks_md, "Inbox", block)
        vaultfs.paths.tasks_file.write_text(new_md, encoding="utf-8")

    return {"source_file": file_name, "extracted": extracted, "written_to": "tasks.md"}


def build_server(vault_root: str) -> FastMCP:
    vaultfs = VaultFS(vault_root)
    mcp = FastMCP("Obsidian Second Brain")

    # -------------------------
    # Files
    # -------------------------
    @mcp.tool()
    def read_file(file_name: str) -> dict[str, Any]:
        params = {"file_name": file_name}

        def _impl() -> dict[str, Any]:
            content = vaultfs.read_text(file_name)
            return ok({"file_name": file_name, "content": content})

        return _tool_autolog(vaultfs, "read_file", params, _impl)

    @mcp.tool()
    def write_file(file_name: str, content: str) -> dict[str, Any]:
        params = {"file_name": file_name, "content_len": len(content or "")}

        def _impl() -> dict[str, Any]:
            bytes_written, created = vaultfs.write_text(file_name, content or "")
            return ok({"file_name": file_name, "bytes_written": bytes_written, "created": created})

        return _tool_autolog(vaultfs, "write_file", params, _impl)

    @mcp.tool()
    def append_to_file(file_name: str, content: str) -> dict[str, Any]:
        params = {"file_name": file_name, "content_len": len(content or "")}

        def _impl() -> dict[str, Any]:
            bytes_appended, created = vaultfs.append_text(file_name, content or "")
            return ok({"file_name": file_name, "bytes_appended": bytes_appended, "created": created})

        return _tool_autolog(vaultfs, "append_to_file", params, _impl)

    @mcp.tool()
    def list_files(folder: Optional[str] = None) -> dict[str, Any]:
        params = {"folder": folder}

        def _impl() -> dict[str, Any]:
            files = vaultfs.list_md_files(folder)
            return ok({"folder": folder or "", "files": files})

        return _tool_autolog(vaultfs, "list_files", params, _impl)

    # -------------------------
    # Search
    # -------------------------
    @mcp.tool()
    def search_notes(query: str, folder: Optional[str] = None) -> dict[str, Any]:
        params = {"query": query, "folder": folder}

        def _impl() -> dict[str, Any]:
            return ok(_search_notes_impl(vaultfs, query, folder))

        return _tool_autolog(vaultfs, "search_notes", params, _impl)

    # -------------------------
    # Daily note
    # -------------------------
    @mcp.tool()
    def create_daily_note() -> dict[str, Any]:
        params: dict[str, Any] = {}

        def _impl() -> dict[str, Any]:
            day = _today_local()
            vaultfs.paths.daily_journal_dir.mkdir(parents=True, exist_ok=True)
            rel = f"Daily Journal/{_iso_date(day)}.md"
            p = vaultfs.safe_path(rel)
            if p.exists():
                # Не перезаписываем
                return ok({"file_name": rel, "status": "exists", "template_used": "existing"})

            tpl_path = vaultfs.paths.templates_dir / "daily-note.md"
            if tpl_path.exists() and tpl_path.is_file():
                template = tpl_path.read_text(encoding="utf-8")
                template_used = "Templates/daily-note.md"
            else:
                template = DEFAULT_DAILY_TEMPLATE
                template_used = "default"

            content = _render_daily_template(template, day)
            vaultfs.write_text(rel, content)
            return ok({"file_name": rel, "status": "created", "template_used": template_used})

        return _tool_autolog(vaultfs, "create_daily_note", params, _impl)

    # -------------------------
    # Tasks
    # -------------------------
    @mcp.tool()
    def add_task(
        description: str,
        due_date: Optional[str] = None,
        tags: Optional[list[str]] = None,
        priority: Priority = "none",
    ) -> dict[str, Any]:
        params = {"description": description, "due_date": due_date, "tags": tags or [], "priority": priority}

        def _impl() -> dict[str, Any]:
            _ensure_tasks_file(vaultfs)
            line, task_id = _build_task_line(description, due_date, tags or [], priority)
            md = vaultfs.paths.tasks_file.read_text(encoding="utf-8")
            new_md, inserted_under = _insert_under_heading(md, "Inbox", line)
            vaultfs.paths.tasks_file.write_text(new_md, encoding="utf-8")
            return ok(
                {
                    "file_name": "tasks.md",
                    "task_text": line,
                    "task_id": task_id,
                    "inserted_under": inserted_under,
                }
            )

        return _tool_autolog(vaultfs, "add_task", params, _impl)

    @mcp.tool()
    def complete_task(file_name: str, task_line_or_text: str) -> dict[str, Any]:
        params = {"file_name": file_name, "task_line_or_text": task_line_or_text}

        def _impl() -> dict[str, Any]:
            p = vaultfs.safe_path(file_name)
            if not p.exists():
                raise MCPError("NOT_FOUND", "Файл не найден.", {"file_name": file_name})
            text = p.read_text(encoding="utf-8")
            lines = text.splitlines()

            needle = (task_line_or_text or "").strip()
            if not needle:
                raise MCPError("INVALID_ARGUMENT", "task_line_or_text не может быть пустым.", {})

            matches: list[int] = []
            mode: str = "exact_line"

            if needle.startswith("t_"):
                mode = "id"
                for i, line in enumerate(lines):
                    m = ID_RE.search(line)
                    if m and m.group(1) == needle:
                        matches.append(i)
            else:
                for i, line in enumerate(lines):
                    if line.strip() == needle:
                        matches.append(i)

            if not matches:
                raise MCPError("TASK_NOT_FOUND", "Задача не найдена.", {"needle": needle, "mode": mode})
            if len(matches) > 1:
                raise MCPError("AMBIGUOUS_MATCH", "Найдено несколько совпадений задачи.", {"matches": len(matches)})

            idx = matches[0]
            old = lines[idx]
            if re.search(r"-\s*\[\s*x\s*\]", old, flags=re.IGNORECASE):
                # уже выполнена
                return ok(
                    {
                        "file_name": file_name,
                        "completed": True,
                        "matched_mode": mode,
                        "completed_date": _iso_date(_today_local()),
                        "new_task_text": old.strip(),
                    }
                )

            new = re.sub(r"-\s*\[\s*\]", "- [x]", old, count=1)
            done_tag = f"✅ {_iso_date(_today_local())}"
            if "✅" not in new:
                new = new.rstrip() + " " + done_tag
            lines[idx] = new
            p.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            return ok(
                {
                    "file_name": file_name,
                    "completed": True,
                    "matched_mode": mode,
                    "completed_date": _iso_date(_today_local()),
                    "new_task_text": new.strip(),
                }
            )

        return _tool_autolog(vaultfs, "complete_task", params, _impl)

    @mcp.tool()
    def get_open_tasks(due_before: Optional[str] = None, tags: Optional[list[str]] = None) -> dict[str, Any]:
        params = {"due_before": due_before, "tags": tags or []}

        def _impl() -> dict[str, Any]:
            return ok(_get_open_tasks_impl(vaultfs, due_before, tags))

        return _tool_autolog(vaultfs, "get_open_tasks", params, _impl)

    # -------------------------
    # Weekly review
    # -------------------------
    @mcp.tool()
    def generate_weekly_review(week_start: Optional[str] = None) -> dict[str, Any]:
        params = {"week_start": week_start}

        def _impl() -> dict[str, Any]:
            return ok(_generate_weekly_review_impl(vaultfs, week_start))

        return _tool_autolog(vaultfs, "generate_weekly_review", params, _impl)

    # -------------------------
    # Task extraction
    # -------------------------
    @mcp.tool()
    def extract_tasks_from_note(file_name: str) -> dict[str, Any]:
        params = {"file_name": file_name}

        def _impl() -> dict[str, Any]:
            return ok(_extract_tasks_from_note_impl(vaultfs, file_name))

        return _tool_autolog(vaultfs, "extract_tasks_from_note", params, _impl)

    # -------------------------
    # Logging tools (explicit)
    # -------------------------
    @mcp.tool()
    def log_action(action_description: str, details: Any = None) -> dict[str, Any]:
        params = {"action_description": action_description, "details": details}

        def _impl() -> dict[str, Any]:
            lf = _append_actions_log(
                vaultfs,
                "log_action",
                params,
                "✅ успех",
                f"{(action_description or '').strip() or '—'}",
            )
            return ok({"log_file": lf, "written": True})

        # Важно: log_action тоже логируется, но без рекурсии (мы тут не вызываем _tool_autolog повторно)
        return _tool_autolog(vaultfs, "log_action", params, _impl)

    @mcp.tool()
    def log_chat_report(user_message: str, ai_actions: Any, summary: str) -> dict[str, Any]:
        params = {"user_message_len": len(user_message or ""), "summary_len": len(summary or "")}

        def _impl() -> dict[str, Any]:
            vaultfs.paths.cursor_logs_dir.mkdir(parents=True, exist_ok=True)
            report_rel = f"Cursor Logs/chat-report-{_now_local_compact()}.md"
            report_path = vaultfs.safe_path(report_rel)
            vaultfs.ensure_parent_dir(report_path)

            body = []
            body.append(f"# Chat report {_now_local_compact()}")
            body.append("")
            body.append("## Сообщение пользователя")
            body.append(_ensure_utf8_text(user_message or "").rstrip() or "—")
            body.append("")
            body.append("## Действия AI")
            try:
                body.append("```json")
                body.append(json.dumps(ai_actions, ensure_ascii=False, indent=2))
                body.append("```")
            except Exception:
                body.append(str(ai_actions))
            body.append("")
            body.append("## Итог")
            body.append(_ensure_utf8_text(summary or "").rstrip() or "—")
            body.append("")

            report_path.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")

            # Линкуем из дневного лога действий
            lf = _append_actions_log(
                vaultfs,
                "log_chat_report",
                {"report_file": report_rel},
                "✅ успех",
                f"Отчёт записан в `{report_rel}`",
            )

            return ok({"report_file": report_rel, "linked_from_daily_log": True, "daily_log": lf})

        return _tool_autolog(vaultfs, "log_chat_report", params, _impl)

    return mcp


def _self_test(vault_root: str) -> int:
    vaultfs = VaultFS(vault_root)
    # Минимальная структура
    (vaultfs.vault / "Inbox").mkdir(parents=True, exist_ok=True)
    (vaultfs.vault / "Daily Journal").mkdir(parents=True, exist_ok=True)
    (vaultfs.vault / "Weekly Reviews").mkdir(parents=True, exist_ok=True)
    (vaultfs.vault / "Templates").mkdir(parents=True, exist_ok=True)

    # 1) daily note
    day = _today_local()
    daily_rel = f"Daily Journal/{_iso_date(day)}.md"
    if not vaultfs.safe_path(daily_rel).exists():
        vaultfs.write_text(daily_rel, _render_daily_template(DEFAULT_DAILY_TEMPLATE, day))
    _append_actions_log(vaultfs, "self_test", {"step": "create_daily_note"}, "✅ успех", f"Проверен `{daily_rel}`")

    # 2) add tasks
    _ensure_tasks_file(vaultfs)
    line1, _ = _build_task_line("Дописать ТЗ", _iso_date(day + dt.timedelta(days=1)), ["work"], "high")
    md = vaultfs.paths.tasks_file.read_text(encoding="utf-8")
    md, _ = _insert_under_heading(md, "Inbox", line1)
    vaultfs.paths.tasks_file.write_text(md, encoding="utf-8")
    _append_actions_log(vaultfs, "self_test", {"step": "add_task"}, "✅ успех", "Добавлена тестовая задача в `tasks.md`")

    # 3) search
    hits = _search_notes_impl(vaultfs, "ТЗ", None)["hits"]
    _append_actions_log(vaultfs, "self_test", {"step": "search_notes"}, "✅ успех", f"Найдено совпадений: {len(hits)}")

    # 4) extract tasks from note
    vaultfs.write_text("Inbox/meeting.md", "Нужно сделать прототип к пятнице.\nДоговорились о созвоне до конца месяца.\n")
    extracted = _extract_tasks_from_note_impl(vaultfs, "Inbox/meeting.md")["extracted"]
    _append_actions_log(
        vaultfs,
        "self_test",
        {"step": "extract_tasks_from_note"},
        "✅ успех",
        f"Извлечено задач: {len(extracted)} (см. `tasks.md`)",
    )

    # 5) weekly review
    wr = _generate_weekly_review_impl(vaultfs, None)
    _append_actions_log(
        vaultfs,
        "self_test",
        {"step": "generate_weekly_review"},
        "✅ успех",
        f"Создан/проверен `{wr['file_name']}`",
    )

    # 6) ensure log file exists
    lf = _actions_log_file(vaultfs, day)
    if not lf.exists():
        lf.write_text("## self-test\n\n---\n", encoding="utf-8")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Obsidian Second Brain MCP server (Cursor stdio)")
    parser.add_argument("--vault", required=True, help="Путь к Obsidian vault (папка с .md)")
    parser.add_argument("--self-test", action="store_true", help="Прогнать быстрый smoke-тест и выйти")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test(args.vault)

    try:
        server = build_server(args.vault)
    except MCPError as e:
        print(f"Ошибка: {e.message}", file=sys.stderr)
        if e.details:
            print(f"Детали: {json.dumps(e.details, ensure_ascii=False)}", file=sys.stderr)
        return 2
    # stdio loop
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
