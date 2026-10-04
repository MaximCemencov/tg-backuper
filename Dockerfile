# tg-backuper: Python 3.12 + age + клиент PostgreSQL + docker CLI.
# Клиент PostgreSQL новее сервера — это нормально: pg_dump 17 снимает и 16, и 15.
# Лишнее можно не ставить: --build-arg PG_CLIENT= и/или --build-arg DOCKER_CLI=false.
FROM python:3.12-slim-bookworm

ARG PG_CLIENT=17
ARG DOCKER_CLI=true

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg age tzdata; \
    if [ -n "$PG_CLIENT" ]; then \
      install -d /usr/share/postgresql-common/pgdg; \
      curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
      echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    fi; \
    if [ "$DOCKER_CLI" = "true" ]; then \
      install -m 0755 -d /etc/apt/keyrings; \
      curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc; \
      echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ${PG_CLIENT:+postgresql-client-$PG_CLIENT} \
      $( [ "$DOCKER_CLI" = "true" ] && echo docker-ce-cli ); \
    apt-get purge -y --auto-remove gnupg; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backuper.py /app/backuper.py
COPY tests /app/tests
RUN python -m unittest discover -s tests && rm -rf /app/tests

ENV PYTHONUNBUFFERED=1 BACKUPER_CONFIG=/config/config.toml
VOLUME ["/data"]
ENTRYPOINT ["python", "/app/backuper.py"]
CMD ["daemon"]
