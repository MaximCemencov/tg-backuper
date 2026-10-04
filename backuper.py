#!/usr/bin/env python3
"""tg-backuper — бэкапы в Telegram без мусора в чате.

По расписанию собирает файлы (каталоги, вывод команд, загрузки по ссылке), при желании
шифрует их age, отправляет в чат одним альбомом и удаляет из чата прошлый набор.
К Telegram подключается только на время отправки: Bot API — обычный HTTPS без
постоянного соединения, поэтому один бот обслуживает сколько угодно машин и заданий.

Только стандартная библиотека Python 3.11+. Внешние программы — по необходимости:
age (шифрование), pg_dump, docker и всё, что вызывают ваши команды.
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "1.0.0"
DEFAULT_CONFIG = "/config/config.toml"
# Облачный Bot API принимает файлы до 50 МБ. Части по 45 МиБ проходят с запасом.
DEFAULT_PART_MB = 45
MAX_ALBUM = 10          # Telegram: не больше 10 файлов в одном альбоме
MAX_CAPTION = 1024


class ConfigError(Exception):
    pass


class TelegramError(Exception):
    def __init__(self, method: str, description: str, code: int | None = None):
        super().__init__(f"{method}: {description}")
        self.code = code
        self.description = description


def log(msg: str) -> None:
    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S%z} {msg}", flush=True)


# ── Тексты в чат ────────────────────────────────────────────────────────────

TEXTS = {
    "ru": {
        "part": "часть {i} из {n}",
        "took": "за {s} с",
        "parts": "частей: {n}",
        "encrypted": "🔒 age",
        "failed_all": "Ни один источник не собрался — прошлый бэкап оставлен в чате.",
        "failed_some": "Не всё собралось — прошлый бэкап оставлен в чате.",
        "send_failed": "Отправка не удалась: {err}",
        "replaced": "Заменено более новым бэкапом",
        "join": "Собрать: cat {name}.part* > {name}",
    },
    "en": {
        "part": "part {i} of {n}",
        "took": "in {s} s",
        "parts": "parts: {n}",
        "encrypted": "🔒 age",
        "failed_all": "No source succeeded — the previous backup stays in the chat.",
        "failed_some": "Some sources failed — the previous backup stays in the chat.",
        "send_failed": "Sending failed: {err}",
        "replaced": "Replaced by a newer backup",
        "join": "Join: cat {name}.part* > {name}",
    },
}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ── Расписание ──────────────────────────────────────────────────────────────

CRON_MACROS = {
    "@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
CRON_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _cron_field(text: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        step, stepped = 1, "/" in part
        if stepped:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ConfigError(f"шаг в cron должен быть ≥ 1: {text}")
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = int(part)
            b = hi if stepped else a        # «5/10» — с пятой каждые десять
        if a < lo or b > hi or a > b:
            raise ConfigError(f"значение вне диапазона {lo}-{hi}: {text}")
        values.update(range(a, b + 1, step))
    return values


class Schedule:
    """Cron из пяти полей (минута час день месяц день_недели) или интервал ("6h")."""

    def __init__(self, cron: str | None = None, interval: str | None = None,
                 tz: ZoneInfo | None = None):
        if bool(cron) == bool(interval):
            raise ConfigError("у задания должно быть ровно одно из: schedule или interval")
        self.tz = tz or ZoneInfo("UTC")
        self.interval: timedelta | None = None
        self.text = cron or interval or ""
        if interval:
            self.interval = parse_interval(interval)
            return
        expr = CRON_MACROS.get(cron.strip(), cron.strip())
        fields = expr.split()
        if len(fields) != 5:
            raise ConfigError(f"в cron нужно 5 полей: {cron!r}")
        try:
            sets = [_cron_field(f, lo, hi) for f, (lo, hi) in zip(fields, CRON_BOUNDS)]
        except ValueError as e:
            raise ConfigError(f"не разобрать cron {cron!r}: {e}") from None
        self.minutes, self.hours, self.days, self.months, dows = sets
        self.dows = {0 if d == 7 else d for d in dows}
        self.dom_any = fields[2] == "*"
        self.dow_any = fields[4] == "*"

    def _day_ok(self, d: datetime) -> bool:
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow
        if self.dow_any:
            return dom
        return dom or dow        # как в cron: если заданы оба — достаточно одного

    def next_after(self, after: datetime) -> datetime:
        """Ближайший момент строго позже `after`."""
        if self.interval is not None:
            return after + self.interval
        t = after.astimezone(self.tz).replace(second=0, microsecond=0, tzinfo=None)
        t += timedelta(minutes=1)
        limit = t + timedelta(days=366 * 5)
        while t < limit:
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if t.hour not in self.hours:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            aware = t.replace(tzinfo=self.tz)
            # Несуществующее время при переходе на летнее: zoneinfo сдвигает его,
            # такой момент пропускаем, как пропустил бы cron.
            if aware.astimezone(ZoneInfo("UTC")).astimezone(self.tz).replace(tzinfo=None) != t:
                t += timedelta(minutes=1)
                continue
            return aware
        raise ConfigError(f"расписание {self.text!r} никогда не срабатывает")


def parse_interval(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", text or "")
    if not m:
        raise ConfigError(f"интервал вида 30m, 6h, 1d, 1w — а не {text!r}")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ConfigError("интервал должен быть больше нуля")
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    return timedelta(**{units[unit]: n})


# ── Настройки ───────────────────────────────────────────────────────────────

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value, env: dict[str, str]):
    """${VAR} и ${VAR:-по_умолчанию} из окружения. Команды не трогаем: их раскрывает bash."""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise ConfigError(f"не задана переменная окружения {name}")
        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: (v if k == "command" else expand(v, env)) for k, v in value.items()}
    return value


@dataclass
class Source:
    name: str
    kind: str                       # path | command | url
    target: str
    gzip: bool = False
    timeout: int = 3600
    run_as_uid: int | None = None
    run_as_gid: int | None = None
    run_as_owner_of: str | None = None
    headers: dict = field(default_factory=dict)
    exclude: list = field(default_factory=list)


@dataclass
class Job:
    name: str
    schedule: Schedule
    sources: list[Source]
    token: str
    chat_id: str
    api_url: str = "https://api.telegram.org"
    thread_id: int | None = None
    silent: bool = False
    keep: int = 1
    title: str | None = None
    age_recipients: list[str] = field(default_factory=list)
    part_size: int = DEFAULT_PART_MB * 1024 * 1024
    run_on_start: bool = False


@dataclass
class Config:
    jobs: list[Job]
    data_dir: Path
    host: str
    lang: str
    tz: ZoneInfo


def _safe_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        raise ConfigError(f"имя {name!r}: только латиница, цифры, точка, дефис и подчёркивание")
    return name


def _int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"ожидалось число, а не {v!r}") from None


def load_config(path: str, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"нет файла настроек {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"ошибка в {path}: {e}") from None
    raw = expand(raw, env)

    tz = ZoneInfo(raw.get("timezone") or env.get("TZ") or "UTC")
    lang = raw.get("lang", "ru")
    if lang not in TEXTS:
        raise ConfigError(f"lang: {lang!r}, доступны {', '.join(TEXTS)}")
    tg = raw.get("telegram", {})
    jobs_raw = raw.get("job", [])
    if not jobs_raw:
        raise ConfigError("нет ни одного задания [[job]]")

    jobs: list[Job] = []
    names: set[str] = set()
    for j in jobs_raw:
        name = _safe_name(j.get("name", ""))
        if name in names:
            raise ConfigError(f"задание {name} описано дважды")
        names.add(name)
        jt = {**tg, **j.get("telegram", {})}
        token, chat_id = str(jt.get("token", "")), str(jt.get("chat_id", ""))
        if not token or not chat_id:
            raise ConfigError(f"{name}: нужны telegram.token и telegram.chat_id")
        sources: list[Source] = []
        snames: set[str] = set()
        for s in j.get("source", []):
            sname = _safe_name(s.get("name", ""))
            if sname in snames:
                raise ConfigError(f"{name}: источник {sname} описан дважды")
            snames.add(sname)
            kinds = [k for k in ("path", "command", "url") if k in s]
            if len(kinds) != 1:
                raise ConfigError(f"{name}/{sname}: нужно ровно одно из path, command, url")
            sources.append(Source(
                name=sname, kind=kinds[0], target=str(s[kinds[0]]),
                gzip=bool(s.get("gzip", False)), timeout=int(s.get("timeout", 3600)),
                run_as_uid=_int_or_none(s.get("run_as_uid")),
                run_as_gid=_int_or_none(s.get("run_as_gid")),
                run_as_owner_of=s.get("run_as_owner_of"),
                headers=dict(s.get("headers", {})), exclude=list(s.get("exclude", []))))
        if not sources:
            raise ConfigError(f"{name}: нет ни одного источника [[job.source]]")
        part_mb = float(j.get("part_size_mb", raw.get("part_size_mb", DEFAULT_PART_MB)))
        keep = int(j.get("keep", 1))
        if keep < 1:
            raise ConfigError(f"{name}: keep должен быть ≥ 1")
        jobs.append(Job(
            name=name,
            schedule=Schedule(j.get("schedule"), j.get("interval"), tz),
            sources=sources, token=token, chat_id=chat_id,
            api_url=str(jt.get("api_url", "https://api.telegram.org")).rstrip("/"),
            thread_id=int(jt["thread_id"]) if jt.get("thread_id") not in (None, "") else None,
            silent=str(jt.get("silent", False)).lower() in ("1", "true", "yes"),
            keep=keep, title=j.get("title"),
            age_recipients=list(j.get("age_recipients", raw.get("age_recipients", []))),
            part_size=max(1024, int(part_mb * 1024 * 1024)),
            run_on_start=bool(j.get("run_on_start", False))))

    return Config(jobs=jobs, data_dir=Path(raw.get("data_dir", "/data")),
                  host=str(raw.get("host") or socket.gethostname()), lang=lang, tz=tz)


# ── Telegram ────────────────────────────────────────────────────────────────

class _Multipart:
    """Тело multipart/form-data, которое читается из файлов кусками, а не целиком в память."""

    CHUNK = 1024 * 1024

    def __init__(self, fields: dict[str, str], files: dict[str, Path]):
        self.boundary = "----tgbackuper" + uuid.uuid4().hex
        self._items: list[bytes | Path] = []
        for k, v in fields.items():
            self._items.append(
                f'--{self.boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'
                f"{v}\r\n".encode())
        for k, p in files.items():
            fname = p.name.replace('"', "")
            self._items.append(
                f'--{self.boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
            self._items.append(p)
            self._items.append(b"\r\n")
        self._items.append(f"--{self.boundary}--\r\n".encode())

    def __len__(self) -> int:
        return sum(i.stat().st_size if isinstance(i, Path) else len(i) for i in self._items)

    def __iter__(self):
        for i in self._items:
            if isinstance(i, Path):
                with open(i, "rb") as f:
                    while chunk := f.read(self.CHUNK):
                        yield chunk
            else:
                yield i


class Telegram:
    def __init__(self, token: str, api_url: str = "https://api.telegram.org",
                 timeout: int = 900, retries: int = 4):
        self.base = f"{api_url}/bot{token}"
        self.timeout = timeout
        self.retries = retries

    def call(self, method: str, fields: dict | None = None,
             files: dict[str, Path] | None = None):
        fields = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                  for k, v in (fields or {}).items() if v is not None}
        for attempt in range(1, self.retries + 1):
            if files:
                body = _Multipart(fields, files)
                req = urllib.request.Request(f"{self.base}/{method}", data=iter(body),
                                             method="POST")
                req.add_header("Content-Type", f"multipart/form-data; boundary={body.boundary}")
                req.add_header("Content-Length", str(len(body)))
            else:
                # Без файлов — обычная форма: пустой multipart Telegram отвергает (400).
                data = urllib.parse.urlencode(fields).encode()
                req = urllib.request.Request(f"{self.base}/{method}", data=data, method="POST")
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read().decode())
                except Exception:
                    data = {"ok": False, "error_code": e.code, "description": f"HTTP {e.code}"}
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt == self.retries:
                    raise TelegramError(method, f"нет связи с Telegram: {e}") from None
                time.sleep(min(60, 5 * attempt))
                continue
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code")
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if code == 429 and retry_after and attempt < self.retries:
                time.sleep(int(retry_after) + 1)
                continue
            if code and code >= 500 and attempt < self.retries:
                time.sleep(min(60, 5 * attempt))
                continue
            raise TelegramError(method, str(data.get("description", "ошибка")), code)
        raise TelegramError(method, "исчерпаны попытки")

    @staticmethod
    def pack(docs: list[tuple[Path, str]], max_bytes: int) -> list[list[tuple[Path, str]]]:
        """Делит файлы на альбомы: не больше 10 штук и не больше max_bytes на запрос.

        Облачный Bot API обрывает запрос больше ~50 МБ целиком, а не только файл
        больше 50 МБ: альбом из трёх дампов по 30 МБ не уходит."""
        groups: list[list[tuple[Path, str]]] = []
        cur: list[tuple[Path, str]] = []
        cur_bytes = 0
        for doc in docs:
            size = doc[0].stat().st_size
            if cur and (len(cur) == MAX_ALBUM or cur_bytes + size > max_bytes):
                groups.append(cur)
                cur, cur_bytes = [], 0
            cur.append(doc)
            cur_bytes += size
        if cur:
            groups.append(cur)
        return groups

    def send_documents(self, chat_id: str, docs: list[tuple[Path, str]],
                       thread_id: int | None = None, silent: bool = False,
                       ids: list[int] | None = None,
                       max_bytes: int = DEFAULT_PART_MB * 1024 * 1024) -> list[int]:
        """Отправляет файлы альбомами (см. pack). Подписи — HTML.

        id отправленных сообщений дописываются в `ids` по ходу дела, чтобы при обрыве
        на середине было что убрать."""
        ids = [] if ids is None else ids
        for group in self.pack(docs, max_bytes):
            common = {"chat_id": chat_id, "message_thread_id": thread_id,
                      "disable_notification": "true" if silent else None}
            if len(group) == 1:
                path, caption = group[0]
                msg = self.call("sendDocument", {**common, "caption": caption[:MAX_CAPTION],
                                                 "parse_mode": "HTML"}, {"document": path})
                ids.append(msg["message_id"])
                continue
            media, files = [], {}
            for i, (path, caption) in enumerate(group):
                key = f"f{i}"
                files[key] = path
                item = {"type": "document", "media": f"attach://{key}"}
                if caption:
                    item.update(caption=caption[:MAX_CAPTION], parse_mode="HTML")
                media.append(item)
            msgs = self.call("sendMediaGroup", {**common, "media": media}, files)
            ids.extend(m["message_id"] for m in msgs)
        return ids

    def send_text(self, chat_id: str, text: str, thread_id: int | None = None,
                  silent: bool = False) -> int:
        msg = self.call("sendMessage", {
            "chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
            "message_thread_id": thread_id, "disable_notification": "true" if silent else None})
        return msg["message_id"]

    def delete(self, chat_id: str, ids: list[int], one_by_one: bool = False) -> list[int]:
        """Удаляет сообщения. Возвращает те, что удалить не вышло.

        Бот может удалять свои сообщения, только пока им меньше 48 часов. Для старых
        наборов `one_by_one`: пакетный deleteMessages молча пропускает то, что не нашёл,
        а по одному видно каждый отказ.
        """
        failed: list[int] = []
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            if not one_by_one:
                try:
                    self.call("deleteMessages", {"chat_id": chat_id, "message_ids": chunk})
                    continue
                except TelegramError:
                    pass
            for mid in chunk:      # по одному: так видно, какие именно не удаляются
                try:
                    self.call("deleteMessage", {"chat_id": chat_id, "message_id": mid})
                except TelegramError as e:
                    if "not found" in e.description.lower():
                        continue
                    failed.append(mid)
        return failed

    def replace_with_stub(self, chat_id: str, mid: int, kind: str, text: str,
                          stub: Path) -> bool:
        """Сообщение старше 48 часов не удалить, но своё можно отредактировать в любой
        момент: файл заменяется заглушкой, и старые данные из чата пропадают."""
        try:
            if kind == "text":
                self.call("editMessageText", {"chat_id": chat_id, "message_id": mid,
                                              "text": text})
            else:
                self.call("editMessageMedia", {
                    "chat_id": chat_id, "message_id": mid,
                    "media": {"type": "document", "media": "attach://stub", "caption": text}},
                    {"stub": stub})
            return True
        except TelegramError as e:
            return "not modified" in e.description.lower()


# ── Состояние ───────────────────────────────────────────────────────────────

class State:
    """Какие сообщения в чате принадлежат заданию. Хранится на этой машине."""

    def __init__(self, data_dir: Path, job: str):
        self.path = data_dir / "state" / f"{job}.json"
        self.sets: list[dict] = []
        if self.path.exists():
            self.sets = json.loads(self.path.read_text()).get("sets", [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sets": self.sets}, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)


# ── Сбор файлов ─────────────────────────────────────────────────────────────

@dataclass
class Artifact:
    source: Source
    path: Path | None = None
    size: int = 0
    sha256: str = ""
    error: str | None = None
    seconds: float = 0.0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _tail(text: str, n: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def _excluded(rel: str, patterns: list[str]) -> bool:
    """Шаблон срабатывает на сам путь и на любой его родительский каталог."""
    parts = rel.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return any(fnmatch.fnmatch(pre, pat) for pre in prefixes for pat in patterns)


def resolve_ids(src: Source) -> tuple[int | None, int | None]:
    """uid/gid для команды. run_as_owner_of — от имени владельца файла: так peer-вход в
    PostgreSQL через сокет работает на любой машине, где у postgres свой uid."""
    if src.run_as_owner_of:
        st = os.stat(src.run_as_owner_of)
        return st.st_uid, st.st_gid
    return src.run_as_uid, src.run_as_gid


def collect(src: Source, workdir: Path) -> Artifact:
    art = Artifact(src)
    t0 = time.monotonic()
    try:
        if src.kind == "path":
            p = Path(src.target)
            if p.is_dir():
                out = workdir / (src.name if src.name.endswith((".tar.gz", ".tgz"))
                                 else f"{src.name}.tar.gz")
                with tarfile.open(out, "w:gz", compresslevel=6) as tar:
                    for item in sorted(p.rglob("*")):
                        rel = item.relative_to(p).as_posix()
                        if _excluded(rel, src.exclude):
                            continue
                        tar.add(item, arcname=f"{p.name}/{rel}", recursive=False)
            elif p.is_file():
                out = workdir / src.name
                shutil.copyfile(p, out)
            else:
                raise RuntimeError(f"нет такого пути: {p}")
        elif src.kind == "command":
            out = workdir / src.name
            uid, gid = resolve_ids(src)
            with open(out, "wb") as f:
                proc = subprocess.Popen(
                    ["/bin/bash", "-o", "pipefail", "-c", src.target],
                    stdout=f, stderr=subprocess.PIPE, start_new_session=True,
                    user=uid, group=gid, extra_groups=[] if uid is not None else None)
                try:
                    _, err = proc.communicate(timeout=src.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    raise RuntimeError(f"не уложилась в {src.timeout} с") from None
            if proc.returncode != 0:
                raise RuntimeError(f"код {proc.returncode}: "
                                   f"{_tail(err.decode(errors='replace')) or 'без вывода'}")
        else:  # url
            out = workdir / src.name
            req = urllib.request.Request(src.target, headers=src.headers)
            with urllib.request.urlopen(req, timeout=src.timeout) as r, open(out, "wb") as f:
                shutil.copyfileobj(r, f, 1024 * 1024)
        if out.stat().st_size == 0:
            raise RuntimeError("пустой результат")
        if src.gzip:
            gz = out.with_name(out.name + ".gz")
            with open(out, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
                shutil.copyfileobj(fi, fo, 1024 * 1024)
            out.unlink()
            out = gz
        art.path = out
    except urllib.error.HTTPError as e:
        art.error = f"HTTP {e.code}"
    except Exception as e:
        art.error = _tail(str(e)) or e.__class__.__name__
    art.seconds = time.monotonic() - t0
    return art


def encrypt(art: Artifact, recipients: list[str]) -> None:
    out = art.path.with_name(art.path.name + ".age")
    cmd = ["age", "-o", str(out)]
    for r in recipients:
        cmd += ["-r", r]
    r = subprocess.run(cmd + [str(art.path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"age: {_tail(r.stderr)}")
    art.path.unlink()
    art.path = out


def split(path: Path, part_size: int) -> list[Path]:
    if path.stat().st_size <= part_size:
        return [path]
    parts = []
    with open(path, "rb") as f:
        i = 0
        while True:
            i += 1
            part = path.with_name(f"{path.name}.part{i:02d}")
            written = 0
            with open(part, "wb") as out:
                while written < part_size:
                    chunk = f.read(min(1024 * 1024, part_size - written))
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
            if written == 0:
                part.unlink()
                break
            parts.append(part)
    path.unlink()
    return parts


# ── Прогон задания ──────────────────────────────────────────────────────────

def run_job(job: Job, cfg: Config, tg: Telegram | None = None) -> bool:
    """Один прогон. True — всё собрано и отправлено."""
    T = TEXTS[cfg.lang]
    tg = tg or Telegram(job.token, job.api_url)
    state = State(cfg.data_dir, job.name)
    started = time.monotonic()
    when = datetime.now(cfg.tz)
    (cfg.data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{job.name}-", dir=cfg.data_dir / "tmp"))
    log(f"[{job.name}] старт: источников {len(job.sources)}")
    new_set = {"sent_at": when.isoformat(timespec="seconds"), "chat_id": job.chat_id,
               "messages": [], "complete": False}
    sent_ids: list[int] = []
    try:
        arts = [collect(s, workdir) for s in job.sources]
        for a in arts:
            if a.error is None and job.age_recipients:
                try:
                    encrypt(a, job.age_recipients)
                except Exception as e:
                    a.error = _tail(str(e))
            if a.error is None:
                a.size = a.path.stat().st_size
                a.sha256 = _sha256(a.path)
                log(f"[{job.name}] {a.path.name}: {human(a.size)} за {a.seconds:.0f} с")
            else:
                log(f"[{job.name}] ОШИБКА {a.source.name}: {a.error}")
        good = [a for a in arts if a.error is None]

        head = f"🗄 <b>{html.escape(job.title or job.name)}</b> · {html.escape(cfg.host)}"
        lines = [head, f"{when:%d.%m.%Y %H:%M} {when.tzname()}"]
        docs: list[tuple[Path, str]] = []
        total_parts = 0
        for a in arts:
            if a.error:
                lines.append(f"❌ {html.escape(a.source.name)} — "
                             f"<code>{html.escape(a.error[:200])}</code>")
                continue
            parts = split(a.path, job.part_size)
            total_parts += len(parts)
            lines.append(f"✅ {html.escape(a.path.name)} — {human(a.size)} · "
                         f"<code>{a.sha256[:12]}</code>"
                         + (f" · {T['parts'].format(n=len(parts))}" if len(parts) > 1 else ""))
            for i, p in enumerate(parts, 1):
                cap = (f"{html.escape(a.path.name)} · {T['part'].format(i=i, n=len(parts))}"
                       if len(parts) > 1 else "")
                if len(parts) > 1 and i == len(parts):
                    cap += "\n" + html.escape(T["join"].format(name=a.path.name))
                docs.append((p, cap))
        foot = []
        if job.age_recipients and good:
            foot.append(T["encrypted"])
        foot.append(T["took"].format(s=f"{time.monotonic() - started:.0f}"))
        lines.append(" · ".join(foot))
        if len(good) < len(arts):
            lines.append("⚠️ " + (T["failed_all"] if not good else T["failed_some"]))
        summary = "\n".join(lines)
        if len(summary) > MAX_CAPTION:
            summary = summary[:MAX_CAPTION - 1] + "…"

        if docs:
            last_path, last_cap = docs[-1]
            docs[-1] = (last_path, (last_cap + "\n\n" if last_cap else "") + summary)
            if len(docs[-1][1]) > MAX_CAPTION:     # подпись части не влезла — сводка важнее
                docs[-1] = (last_path, summary)
            tg.send_documents(job.chat_id, docs, job.thread_id, job.silent, ids=sent_ids,
                              max_bytes=job.part_size)
            new_set["messages"] = [{"id": i, "kind": "doc"} for i in sent_ids]
        else:
            mid = tg.send_text(job.chat_id, summary, job.thread_id, job.silent)
            new_set["messages"] = [{"id": mid, "kind": "text"}]
        new_set["complete"] = len(good) == len(arts)
    except Exception as e:
        log(f"[{job.name}] ОШИБКА отправки: {e}")
        # Наполовину отправленный набор хуже никакого: убираем его, прошлый остаётся.
        if sent_ids:
            tg.delete(job.chat_id, sent_ids)
        try:
            mid = tg.send_text(job.chat_id, f"🗄 <b>{html.escape(job.title or job.name)}</b> · "
                               f"{html.escape(cfg.host)}\n⚠️ " + html.escape(
                                   T["send_failed"].format(err=str(e)[:300])),
                               job.thread_id, job.silent)
            new_set["messages"] = [{"id": mid, "kind": "text"}]
        except Exception as e2:
            log(f"[{job.name}] и сообщить об ошибке не вышло: {e2}")
            new_set["messages"] = []
        new_set["complete"] = False
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if new_set["messages"]:
        state.sets.append(new_set)
    state.save()
    if new_set["complete"]:
        cleanup(job, cfg, tg, state)
    log(f"[{job.name}] {'готово' if new_set['complete'] else 'с ошибками'}: "
        f"{time.monotonic() - started:.0f} с")
    return bool(new_set["complete"])


def cleanup(job: Job, cfg: Config, tg: Telegram, state: State) -> None:
    """Оставляет в чате последние `keep` полных наборов, остальное убирает."""
    complete = [s for s in state.sets if s.get("complete")]
    keep_ids = {id(s) for s in complete[-job.keep:]}
    drop = [s for s in state.sets if id(s) not in keep_ids]
    if not drop:
        return
    stub = cfg.data_dir / "tmp" / "replaced.txt"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(TEXTS[cfg.lang]["replaced"] + "\n")
    now = datetime.now(cfg.tz)
    for s in drop:
        ids = [m["id"] for m in s.get("messages", [])]
        try:
            age_h = (now - datetime.fromisoformat(s["sent_at"])).total_seconds() / 3600
        except (KeyError, ValueError):
            age_h = 999
        failed = set(tg.delete(s["chat_id"], ids, one_by_one=age_h > 47))
        kinds = {m["id"]: m.get("kind", "doc") for m in s.get("messages", [])}
        for mid in failed:
            if not tg.replace_with_stub(s["chat_id"], mid, kinds.get(mid, "doc"),
                                        TEXTS[cfg.lang]["replaced"], stub):
                log(f"[{job.name}] сообщение {mid} не удалить и не заменить — оставлено")
        if failed:
            log(f"[{job.name}] старше 48 ч, заменено заглушкой: {len(failed)}")
        deleted = len(ids) - len(failed)
        if deleted:
            log(f"[{job.name}] удалено старых сообщений: {deleted}")
    state.sets = [s for s in state.sets if id(s) in keep_ids]
    state.save()


# ── Команды ─────────────────────────────────────────────────────────────────

def cmd_check(cfg: Config) -> int:
    ok = True
    if any(j.age_recipients for j in cfg.jobs) and not shutil.which("age"):
        print("✗ задания шифруются, но программы age нет в системе")
        ok = False
    seen: set[tuple[str, str]] = set()
    now = datetime.now(cfg.tz)
    for job in cfg.jobs:
        print(f"• {job.name}: {job.schedule.text}, следующий запуск "
              f"{job.schedule.next_after(now):%d.%m.%Y %H:%M %Z}, источников {len(job.sources)}, "
              f"хранить наборов {job.keep}" + (", шифрование age" if job.age_recipients else ""))
        key = (job.token, job.chat_id)
        if key in seen:
            continue
        seen.add(key)
        tg = Telegram(job.token, job.api_url, retries=1)
        try:
            me = tg.call("getMe")
            chat = tg.call("getChat", {"chat_id": job.chat_id})
            print(f"  ✓ бот @{me.get('username')} видит чат "
                  f"{chat.get('title') or chat.get('username') or chat.get('id')}")
        except TelegramError as e:
            print(f"  ✗ Telegram: {e}")
            ok = False
    return 0 if ok else 1


def cmd_daemon(cfg: Config) -> int:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    now = datetime.now(cfg.tz)
    upcoming = {j.name: (now if j.run_on_start else j.schedule.next_after(now)) for j in cfg.jobs}
    jobs = {j.name: j for j in cfg.jobs}
    log(f"tg-backuper {VERSION}: заданий {len(jobs)}, машина {cfg.host}")
    for name, at in upcoming.items():
        log(f"[{name}] следующий запуск {at:%d.%m.%Y %H:%M %Z}")
    while not stop.is_set():
        now = datetime.now(cfg.tz)
        for name in sorted(upcoming, key=upcoming.get):
            if stop.is_set() or upcoming[name] > now:
                continue
            try:
                run_job(jobs[name], cfg)
            except Exception as e:      # задание не должно ронять демона
                log(f"[{name}] непредвиденная ошибка: {e}")
            upcoming[name] = jobs[name].schedule.next_after(datetime.now(cfg.tz))
            log(f"[{name}] следующий запуск {upcoming[name]:%d.%m.%Y %H:%M %Z}")
        wait = (min(upcoming.values()) - datetime.now(cfg.tz)).total_seconds()
        stop.wait(max(1.0, min(wait, 60.0)))
    log("остановлен")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tg-backuper", description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=os.environ.get("BACKUPER_CONFIG", DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("daemon", help="работать по расписанию (по умолчанию)")
    run = sub.add_parser("run", help="выполнить задания сейчас")
    run.add_argument("jobs", nargs="*", help="имена заданий (по умолчанию все)")
    sub.add_parser("check", help="проверить настройки и доступ бота к чатам")
    sub.add_parser("version")
    args = ap.parse_args(argv)
    if args.cmd == "version":
        print(VERSION)
        return 0
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Ошибка настроек: {e}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        return cmd_check(cfg)
    if args.cmd == "run":
        names = set(args.jobs) or {j.name for j in cfg.jobs}
        unknown = names - {j.name for j in cfg.jobs}
        if unknown:
            print(f"Нет заданий: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        ok = all([run_job(j, cfg) for j in cfg.jobs if j.name in names])
        return 0 if ok else 1
    return cmd_daemon(cfg)


if __name__ == "__main__":
    sys.exit(main())
