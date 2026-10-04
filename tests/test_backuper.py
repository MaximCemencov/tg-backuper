"""Тесты без сети: Telegram подменён локальным HTTP-сервером, который записывает вызовы."""
import email
import email.policy
import json
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backuper as b  # noqa: E402

b.log = lambda msg: None   # тесты без шума в выводе

MSK = ZoneInfo("Europe/Moscow")


class FakeTelegram:
    def __init__(self):
        self.calls = []            # (method, fields, {file_field: bytes})
        self.next_id = 100
        self.undeletable = set()   # id, которые «старше 48 часов»
        self.fail = {}             # метод -> сколько раз отказать (500)
        self.fail_after = {}       # метод -> отказать на N-м вызове
        self.files = {"/files/export.bin": b"remote-file-" * 50}
        self.max_request = None    # байт файлов в одном запросе (облачный API — ~50 МБ)
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = fake.files.get(self.path)
                self.send_response(200 if body else 404)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                method = self.path.rsplit("/", 1)[-1]
                if self.headers["Content-Type"].startswith("application/x-www-form-urlencoded"):
                    from urllib.parse import parse_qsl
                    fields = dict(parse_qsl(raw.decode()))
                    fake.calls.append((method, fields, {}))
                    return self.reply(*fake.answer(method, fields, {}))
                if not raw.strip().replace(b"-", b"").strip():
                    return self.reply(400, {"ok": False, "description": "empty body"})
                msg = email.message_from_bytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw,
                    policy=email.policy.HTTP)
                fields, files = {}, {}
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if part.get_filename():
                        files[name] = part.get_payload(decode=True)
                    else:
                        fields[name] = part.get_payload(decode=True).decode()
                fake.calls.append((method, fields, files))
                if fake.max_request and sum(map(len, files.values())) > fake.max_request:
                    return self.reply(413, {"ok": False, "error_code": 413,
                                            "description": "Request Entity Too Large"})
                self.reply(*fake.answer(method, fields, files))

            def reply(self, code, data):
                out = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _id(self):
        self.next_id += 1
        return self.next_id

    def answer(self, method, fields, files):
        if self.fail.get(method):
            self.fail[method] -= 1
            return 500, {"ok": False, "error_code": 500, "description": "Internal"}
        nth = sum(1 for c in self.calls if c[0] == method)
        if self.fail_after.get(method) == nth:
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: boom"}
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": 1, "username": "testbot"}}
        if method == "getChat":
            return 200, {"ok": True, "result": {"id": fields["chat_id"], "title": "t"}}
        if method in ("sendDocument", "sendMessage"):
            return 200, {"ok": True, "result": {"message_id": self._id()}}
        if method == "sendMediaGroup":
            media = json.loads(fields["media"])
            return 200, {"ok": True, "result": [{"message_id": self._id()} for _ in media]}
        if method == "deleteMessages":
            ids = json.loads(fields["message_ids"])
            if self.undeletable & set(ids):
                return 400, {"ok": False, "error_code": 400,
                             "description": "Bad Request: message can't be deleted"}
            return 200, {"ok": True, "result": True}
        if method == "deleteMessage":
            if int(fields["message_id"]) in self.undeletable:
                return 400, {"ok": False, "error_code": 400,
                             "description": "Bad Request: message can't be deleted for everyone"}
            return 200, {"ok": True, "result": True}
        if method in ("editMessageMedia", "editMessageText"):
            return 200, {"ok": True, "result": {"message_id": int(fields["message_id"])}}
        return 404, {"ok": False, "error_code": 404, "description": "Not Found"}

    def methods(self):
        return [c[0] for c in self.calls]

    def sent_docs(self):
        """(содержимое, подпись) всех отправленных файлов по порядку."""
        out = []
        for method, fields, files in self.calls:
            if method == "sendDocument":
                out.append((files["document"], fields.get("caption", "")))
            elif method == "sendMediaGroup":
                for i, m in enumerate(json.loads(fields["media"])):
                    out.append((files[f"f{i}"], m.get("caption", "")))
        return out

    def close(self):
        self.server.shutdown()


class CronTest(unittest.TestCase):
    def at(self, *a):
        return datetime(*a, tzinfo=MSK)

    def test_daily(self):
        s = b.Schedule("0 3 * * *", tz=MSK)
        self.assertEqual(s.next_after(self.at(2026, 10, 4, 11, 0)), self.at(2026, 10, 5, 3, 0))
        self.assertEqual(s.next_after(self.at(2026, 10, 4, 2, 59)), self.at(2026, 10, 4, 3, 0))
        self.assertEqual(s.next_after(self.at(2026, 10, 4, 3, 0)), self.at(2026, 10, 5, 3, 0))

    def test_steps_ranges_lists(self):
        s = b.Schedule("*/15 9-10 * * *", tz=MSK)
        self.assertEqual(s.next_after(self.at(2026, 10, 4, 9, 14)), self.at(2026, 10, 4, 9, 15))
        self.assertEqual(s.next_after(self.at(2026, 10, 4, 10, 45)), self.at(2026, 10, 5, 9, 0))
        s = b.Schedule("5/20 * * * *", tz=MSK)
        self.assertEqual(s.next_after(self.at(2026, 10, 4, 9, 26)), self.at(2026, 10, 4, 9, 45))
        s = b.Schedule("0 0 1,15 * *", tz=MSK)
        self.assertEqual(s.next_after(self.at(2026, 10, 2)), self.at(2026, 10, 15))

    def test_dom_or_dow(self):
        # Заданы и число, и день недели — как в cron, достаточно одного.
        s = b.Schedule("0 0 13 * 5", tz=MSK)
        self.assertEqual(s.next_after(self.at(2026, 10, 4)), self.at(2026, 10, 9))   # пятница
        s = b.Schedule("0 0 * * 7", tz=MSK)                                          # 7 = вс
        self.assertEqual(s.next_after(self.at(2026, 10, 5)), self.at(2026, 10, 11))

    def test_month_and_macros(self):
        self.assertEqual(b.Schedule("@monthly", tz=MSK).next_after(self.at(2026, 10, 4)),
                         self.at(2026, 11, 1))
        self.assertEqual(b.Schedule("0 0 29 2 *", tz=MSK).next_after(self.at(2026, 10, 4)),
                         self.at(2028, 2, 29))

    def test_dst(self):
        berlin = ZoneInfo("Europe/Berlin")
        s = b.Schedule("30 2 * * *", tz=berlin)
        # 29.03.2026 2:30 в Берлине не существует — запуск переносится на следующий день.
        got = s.next_after(datetime(2026, 3, 29, 0, 0, tzinfo=berlin))
        self.assertEqual((got.month, got.day, got.hour, got.minute), (3, 30, 2, 30))

    def test_bad(self):
        for bad in ("* * * *", "61 * * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *"):
            with self.assertRaises(b.ConfigError, msg=bad):
                b.Schedule(bad, tz=MSK)
        with self.assertRaises(b.ConfigError):
            b.Schedule("0 * * * *", "1h", tz=MSK)

    def test_interval(self):
        s = b.Schedule(interval="6h", tz=MSK)
        self.assertEqual(s.next_after(self.at(2026, 10, 4, 1)), self.at(2026, 10, 4, 7))
        for bad in ("0h", "6", "h", "1y"):
            with self.assertRaises(b.ConfigError):
                b.parse_interval(bad)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, text):
        p = self.dir / "c.toml"
        p.write_text(text)
        return str(p)

    BASE = '''
[telegram]
token = "${TOK}"
chat_id = "${CHAT:-42}"
[[job]]
name = "db"
schedule = "@daily"
[[job.source]]
name = "a.dump"
command = "echo ${HOME_SECRET}"
'''

    def test_expand(self):
        cfg = b.load_config(self.write(self.BASE), {"TOK": "t0k"})
        j = cfg.jobs[0]
        self.assertEqual((j.token, j.chat_id), ("t0k", "42"))
        # Команды не раскрываются: это сделает bash в момент запуска.
        self.assertEqual(j.sources[0].target, "echo ${HOME_SECRET}")

    def test_missing_var(self):
        with self.assertRaises(b.ConfigError):
            b.load_config(self.write(self.BASE), {})

    def test_validation(self):
        bad = [
            self.BASE.replace('name = "db"', 'name = "d b"'),
            self.BASE.replace('command = "echo ${HOME_SECRET}"', 'command = "x"\npath = "/y"'),
            self.BASE.replace('schedule = "@daily"', ''),
            self.BASE + '[[job]]\nname = "db"\nschedule = "@daily"\n'
                        '[[job.source]]\nname = "b"\npath = "/x"\n',
            self.BASE.replace('schedule = "@daily"', 'schedule = "@daily"\nkeep = 0'),
        ]
        for text in bad:
            with self.assertRaises(b.ConfigError, msg=text):
                b.load_config(self.write(text), {"TOK": "x"})


class PackTest(unittest.TestCase):
    def test_pack(self):
        d = Path(tempfile.mkdtemp())
        try:
            def f(name, size):
                p = d / name
                p.write_bytes(b"x" * size)
                return (p, "")
            big1, small, big2 = f("a", 34), f("b", 3), f("c", 33)
            groups = b.Telegram.pack([big1, small, big2], 45)
            self.assertEqual([[p.name for p, _ in g] for g in groups], [["a", "b"], ["c"]])
            many = [f(f"m{i}", 1) for i in range(12)]
            self.assertEqual([len(g) for g in b.Telegram.pack(many, 1000)], [10, 2])
        finally:
            shutil.rmtree(d)


class IdsTest(unittest.TestCase):
    def test_owner_of(self):
        d = Path(tempfile.mkdtemp())
        try:
            st = d.stat()
            src = b.Source("x", "command", "true", run_as_owner_of=str(d))
            self.assertEqual(b.resolve_ids(src), (st.st_uid, st.st_gid))
            self.assertEqual(b.resolve_ids(b.Source("x", "command", "true", run_as_uid=5)),
                             (5, None))
        finally:
            shutil.rmtree(d)

    def test_numeric_strings(self):
        self.assertEqual(b._int_or_none("111"), 111)
        self.assertIsNone(b._int_or_none(""))
        with self.assertRaises(b.ConfigError):
            b._int_or_none("postgres")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTelegram()
        self.dir = Path(tempfile.mkdtemp())
        self.src = self.dir / "site"
        (self.src / "cache").mkdir(parents=True)
        (self.src / "index.html").write_text("<h1>hi</h1>")
        (self.src / "cache" / "junk.bin").write_bytes(b"x" * 100)

    def tearDown(self):
        self.tg.close()
        shutil.rmtree(self.dir)

    def config(self, extra_sources="", keep=1, part_kb=1.5, cmd="head -c 5000 /dev/zero"):
        text = f'''
data_dir = "{self.dir / 'data'}"
host = "test-host"
timezone = "Europe/Moscow"
[telegram]
token = "TOKEN"
chat_id = "-100500"
api_url = "{self.tg.url}"
[[job]]
name = "nightly"
title = "Ночной"
schedule = "0 3 * * *"
keep = {keep}
part_size_mb = {part_kb / 1024}
[[job.source]]
name = "site"
path = "{self.src}"
exclude = ["cache"]
[[job.source]]
name = "zeros.bin"
command = "{cmd}"
gzip = false
[[job.source]]
name = "export.bin"
url = "{self.tg.url}/files/export.bin"
{extra_sources}
'''
        p = self.dir / "c.toml"
        p.write_text(text)
        cfg = b.load_config(str(p), {})
        return cfg, cfg.jobs[0]

    def run_once(self, cfg, job):
        return b.run_job(job, cfg, b.Telegram(job.token, job.api_url, retries=1))

    def state(self):
        return json.loads((self.dir / "data" / "state" / "nightly.json").read_text())["sets"]

    def test_first_run_sends_and_splits(self):
        cfg, job = self.config()
        self.tg.max_request = job.part_size
        self.assertTrue(self.run_once(cfg, job))
        docs = self.tg.sent_docs()
        # site.tar.gz + 4 части по 1.5 КиБ от 5000 байт + export.bin (600 байт)
        self.assertEqual(len(docs), 1 + 4 + 1)
        self.assertIn("Ночной", docs[-1][1])
        self.assertIn("test-host", docs[-1][1])
        self.assertEqual(b"".join(d for d, _ in docs[1:5]), b"\0" * 5000)
        # ни один запрос не превысил предел, мелкие файлы при этом склеены в альбом
        self.assertNotIn("Request Entity Too Large", json.dumps(self.state()))
        self.assertIn("sendMediaGroup", self.tg.methods())
        sets = self.state()
        self.assertEqual(len(sets), 1)
        self.assertTrue(sets[0]["complete"])
        self.assertEqual(len(sets[0]["messages"]), 6)
        # Временные файлы убраны.
        self.assertEqual([p for p in (self.dir / "data" / "tmp").iterdir()
                          if p.name.startswith("nightly-")], [])

    def test_exclude_parent_dir(self):
        cfg, job = self.config()
        self.run_once(cfg, job)
        import io
        import tarfile
        tar_bytes = self.tg.sent_docs()[0][0]
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as t:
            names = t.getnames()
        self.assertIn("site/index.html", names)
        self.assertFalse(any("cache" in n for n in names), names)

    def test_second_run_deletes_previous(self):
        cfg, job = self.config()
        self.run_once(cfg, job)
        first = [m["id"] for m in self.state()[0]["messages"]]
        self.run_once(cfg, job)
        deleted = [json.loads(c[1]["message_ids"]) for c in self.tg.calls
                   if c[0] == "deleteMessages"]
        self.assertEqual(deleted, [first])
        self.assertEqual(len(self.state()), 1)
        self.assertNotEqual([m["id"] for m in self.state()[0]["messages"]], first)

    def test_failed_source_keeps_previous(self):
        cfg, job = self.config()
        self.run_once(cfg, job)
        cfg2, job2 = self.config(cmd="echo bad >&2; exit 3")
        self.assertFalse(self.run_once(cfg2, job2))
        self.assertNotIn("deleteMessages", self.tg.methods())
        sets = self.state()
        self.assertEqual([s["complete"] for s in sets], [True, False])
        caption = self.tg.sent_docs()[-1][1]
        self.assertIn("код 3", caption)
        self.assertIn("bad", caption)
        # Следующий удачный прогон убирает и старый полный, и неполный наборы.
        self.run_once(cfg, job)
        removed = sum(len(json.loads(c[1]["message_ids"])) for c in self.tg.calls
                      if c[0] == "deleteMessages")
        self.assertEqual(removed, sum(len(s["messages"]) for s in sets))
        self.assertEqual(len(self.state()), 1)

    def test_all_sources_failed_sends_text(self):
        cfg, job = self.config(cmd="exit 1")
        job.sources = [s for s in job.sources if s.name == "zeros.bin"]
        self.assertFalse(self.run_once(cfg, job))
        self.assertEqual(self.tg.methods(), ["sendMessage"])
        self.assertEqual(self.state()[0]["messages"][0]["kind"], "text")

    def test_old_messages_get_stub(self):
        cfg, job = self.config()
        self.run_once(cfg, job)
        sets = self.state()
        sets[0]["sent_at"] = (datetime.now(MSK) - timedelta(days=3)).isoformat()
        (self.dir / "data" / "state" / "nightly.json").write_text(json.dumps({"sets": sets}))
        old = [m["id"] for m in sets[0]["messages"]]
        self.tg.undeletable = set(old[:2])
        self.run_once(cfg, job)
        # Старый набор — сразу по одному, без пакетного удаления.
        self.assertNotIn("deleteMessages", self.tg.methods())
        stubs = [int(c[1]["message_id"]) for c in self.tg.calls if c[0] == "editMessageMedia"]
        self.assertEqual(sorted(stubs), sorted(old[:2]))
        self.assertEqual(len(self.state()), 1)

    def test_send_failure_midway_rolls_back(self):
        # 12 файлов → два альбома; второй падает — первый должен быть удалён.
        extra = "".join(f'''
[[job.source]]
name = "f{i}.txt"
command = "echo {i}"
''' for i in range(9))
        cfg, job = self.config(extra_sources=extra, part_kb=1000)
        self.run_once(cfg, job)                       # удачный набор, 12 файлов
        good = self.state()[0]
        self.tg.fail_after["sendMediaGroup"] = 4      # во втором прогоне — второй альбом
        self.assertFalse(self.run_once(cfg, job))
        groups = [c for c in self.tg.calls if c[0] == "sendMediaGroup"]
        rolled = json.loads([c for c in self.tg.calls if c[0] == "deleteMessages"][-1][1]
                            ["message_ids"])
        self.assertEqual(len(rolled), 10)             # первый альбом второго прогона
        sets = self.state()
        self.assertEqual(sets[0], good)               # прошлый набор на месте
        self.assertFalse(sets[1]["complete"])
        self.assertEqual(sets[1]["messages"][0]["kind"], "text")
        self.assertEqual(len(groups), 4)

    def test_check_uses_plain_form(self):
        cfg, job = self.config()
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(b.cmd_check(cfg), 0)
        self.assertIn("@testbot", out.getvalue())
        self.assertEqual(self.tg.methods(), ["getMe", "getChat"])

    def test_keep_two(self):
        cfg, job = self.config(keep=2)
        for _ in range(3):
            self.run_once(cfg, job)
        self.assertEqual(len(self.state()), 2)
        self.assertEqual(len([c for c in self.tg.calls if c[0] == "deleteMessages"]), 1)

    def test_retry_on_500(self):
        cfg, job = self.config()
        self.tg.fail["sendMediaGroup"] = 1
        tg = b.Telegram(job.token, job.api_url, retries=2)
        b.time.sleep, real = (lambda s: None), b.time.sleep
        try:
            self.assertTrue(b.run_job(job, cfg, tg))
        finally:
            b.time.sleep = real

    @unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "нет age")
    def test_age_encryption(self):
        import subprocess
        key = self.dir / "key.txt"
        subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
        pub = [l.split(": ")[1] for l in key.read_text().splitlines() if "public key" in l][0]
        cfg, job = self.config(part_kb=1000)
        job.age_recipients = [pub]
        self.assertTrue(self.run_once(cfg, job))
        docs = self.tg.sent_docs()
        enc = self.dir / "z.age"
        enc.write_bytes(docs[1][0])
        out = subprocess.run(["age", "-d", "-i", str(key), str(enc)], capture_output=True,
                             check=True).stdout
        self.assertEqual(out, b"\0" * 5000)
        self.assertIn("age", docs[-1][1])


if __name__ == "__main__":
    unittest.main()
