# tg-backuper

Бэкапы в Telegram без мусора в чате. По расписанию собирает файлы, при желании шифрует
их и отправляет в чат одним альбомом. Прошлый набор из чата удаляется. Сколько бы раз
он ни сработал, в чате лежит только последний бэкап (или последние `keep` штук).

*English: scheduled backups to a Telegram chat that replace themselves. Each run sends a
fresh album and deletes the previous one, so the chat holds only the latest backup. One
bot serves any number of machines: the Bot API is plain HTTPS, nothing stays connected.
Python stdlib only, optional age encryption, Docker image included.*

## Как работает

1. В назначенное время собирает источники:
   - **path** — файл или каталог (каталог упаковывается в `.tar.gz`);
   - **command** — вывод команды, например `pg_dump`;
   - **url** — файл по ссылке.
2. Если указан ключ — шифрует каждый файл [age](https://age-encryption.org).
3. Файлы больше 45 МиБ режет на части: облачный Bot API принимает до 50 МБ.
4. Отправляет всё одним альбомом. В подписи последнего файла — сводка: что собралось,
   размеры, начало sha256, ошибки.
5. Запоминает id своих сообщений в `data/state/<задание>.json` и удаляет из чата прошлый
   набор.

**Порядок безопасный:** сначала новое, потом удаление старого. Если какой-то источник не
собрался или отправка оборвалась, прошлый бэкап остаётся в чате. Наполовину отправленный
набор убирается сам.

**Несколько машин — один бот.** К Telegram бэкапер подключается только на время отправки,
а не держит постоянное соединение (`getUpdates` не вызывается). Поэтому одного бота можно
ставить на любое число серверов, конфликтов нет. Каждая машина помнит и удаляет только
свои сообщения. Задайте заданиям на разных машинах разные имена.

**Правило 48 часов.** Бот может удалить своё сообщение, только пока ему меньше 48 часов.
Если бэкап реже раза в двое суток, старое сообщение удалить уже нельзя. Тогда его файл
заменяется заглушкой «Заменено более новым бэкапом»: свои сообщения бот может
редактировать в любой момент, поэтому старых данных в чате не остаётся.

## Быстрый старт

```bash
git clone https://github.com/MaximCemencov/tg-backuper.git && cd tg-backuper
cp docker-compose.example.yml docker-compose.yml
cp config.example.toml config.toml        # опишите свои задания
cp .env.example .env                       # токен бота и чат
docker compose build
docker compose run --rm backuper check     # бот видит чат? расписания понятны?
docker compose run --rm backuper run       # прогнать все задания сейчас
docker compose up -d                       # дальше — по расписанию
```

Бота создайте у [@BotFather](https://t.me/BotFather) и добавьте в нужный чат или канал.
Узнать `chat_id`: перешлите сообщение из чата боту
[@userinfobot](https://t.me/userinfobot) или посмотрите
`https://api.telegram.org/bot<токен>/getUpdates` после сообщения боту.

## Настройки

Полный пример с комментариями — [`config.example.toml`](config.example.toml).

| ключ | где | что |
|---|---|---|
| `timezone` | общий | часовой пояс расписаний и дат в подписях |
| `host` | общий | подпись машины в сообщении |
| `lang` | общий | `ru` или `en` |
| `part_size_mb` | общий, задание | размер части, по умолчанию 45 |
| `telegram.token`, `telegram.chat_id` | общий, `[job.telegram]` | бот и чат |
| `telegram.thread_id` | общий, `[job.telegram]` | тема в группе с темами |
| `telegram.silent` | общий, `[job.telegram]` | без звука |
| `telegram.api_url` | общий, `[job.telegram]` | свой [Bot API server](https://github.com/tdlib/telegram-bot-api), там файлы до 2 ГБ |
| `name` | задание | латиница, цифры, `._-`; уникально на машине |
| `schedule` / `interval` | задание | cron из 5 полей (`0 3 * * *`, `@daily`) или `30m`, `6h`, `1d` |
| `keep` | задание | сколько полных наборов держать в чате, по умолчанию 1 |
| `age_recipients` | общий, задание | публичные ключи age — включает шифрование |
| `run_on_start` | задание | прогнать сразу при запуске |
| `path` / `command` / `url` | источник | ровно одно из трёх |
| `gzip` | источник | сжать результат |
| `timeout` | источник | секунд на источник, по умолчанию 3600 |
| `run_as_uid`, `run_as_gid` | источник | от чьего имени выполнить команду |
| `run_as_owner_of` | источник | от имени владельца файла или каталога (uid и gid берутся с него) |
| `exclude` | источник `path` | шаблоны путей внутри каталога |
| `headers` | источник `url` | заголовки запроса |

Секреты держите в `.env`, а в `config.toml` ссылайтесь на них: `${TG_TOKEN}`.

### Источники: примеры

```toml
# PostgreSQL на хосте без пароля: сокет + peer-вход от имени postgres
# (в compose: - /var/run/postgresql:/var/run/postgresql)
[[job.source]]
name = "app.dump"
command = "pg_dump -Fc -h /var/run/postgresql -U postgres app"
run_as_owner_of = "/var/run/postgresql"   # uid владельца сокета — postgres хоста, на любой машине

# PostgreSQL в другом контейнере — его же pg_dump (в compose: docker.sock)
[[job.source]]
name = "panel.dump"
command = "docker exec panel-db pg_dump -U panel -Fc panel"

# MySQL / MariaDB в контейнере
[[job.source]]
name = "shop.sql"
command = "docker exec shop-db sh -c 'mysqldump -uroot -p\"$MYSQL_ROOT_PASSWORD\" shop'"
gzip = true
```

## Шифрование

Бэкапы баз — это данные пользователей. В Telegram их лучше класть зашифрованными.

```bash
age-keygen -o backup.key       # закрытый ключ — ТОЛЬКО у вас, не на сервере
grep public backup.key         # age1… — его в age_recipients
```

На сервере лежит только публичный ключ. Даже со взломанного сервера прочитать старые
бэкапы нельзя.

## Восстановление

```bash
cat app.dump.age.part* > app.dump.age      # если файл приходил частями
sha256sum app.dump.age                      # сверить с началом хеша в подписи
age -d -i backup.key -o app.dump app.dump.age
pg_restore -d app app.dump
```

## Команды

```
backuper.py daemon          # по расписанию (по умолчанию в контейнере)
backuper.py run [ЗАДАНИЕ…]  # выполнить сейчас
backuper.py check           # проверить настройки и доступ бота к чатам
```

## Безопасность

- `docker.sock` в контейнере — это права root на хосте. Подключайте его, только если
  нужен `docker exec`.
- Бэкапер не принимает входящих соединений и не читает сообщения из чата: только
  отправляет и удаляет свои.
- Тесты (`python -m unittest discover -s tests`) идут без сети, с поддельным Telegram, и
  запускаются при каждой сборке образа.

## Лицензия

MIT
