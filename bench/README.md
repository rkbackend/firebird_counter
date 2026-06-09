# Локальный benchmark Firebird 3

В каталоге `bench/` лежит harness для воспроизводимого локального сравнения двух режимов:

- `without_plugin` - Firebird работает без `ProcUsageTrace`
- `with_plugin` - Firebird работает с включенным `ProcUsageTrace`

Профиль плагина выбирается отдельно:

- `aggregates` - обычные агрегаты по процедурам и `sql_kind`
- `sql_text` - обычные агрегаты плюс полные SQL-тексты с новой фичей `enable_sql_text_stats`

## Что делает harness

- создает отдельную benchmark-БД
- создает `1000` процедур `BENCH_PROC_0001 .. BENCH_PROC_1000`
- запускает `64` параллельных клиента `isql-fb`
- смешивает `EXECUTE PROCEDURE` и прямые `CRUD`-операции
- калибрует количество раундов под целевую длительность
- в режиме `both` чередует порядок `without_plugin` / `with_plugin` между итерациями
- делает по умолчанию `5` измерений на каждый режим
- сохраняет JSON-отчет в `bench/results/latest.json`
- в отчёте дополнительно показывает средний размер `spool` и итоговый размер `SQLite`

## Быстрый запуск

Нужны права на изменение `firebird.conf` и перезапуск `firebird3.0`, поэтому удобнее запускать из обычного терминала, где `sudo` может спросить пароль:

```bash
python3 bench/run_benchmark.py \
  --mode both \
  --plugin-profile aggregates \
  --user sysdba \
  --password 'YOUR_PASSWORD'
```

По умолчанию harness использует:

- `64` клиента
- `1000` процедур
- целевую длительность около `150` секунд
- `5` измерений на режим
- workspace: `/tmp/firebird_proc_usage_benchmark`

## Полезные параметры

```bash
python3 bench/run_benchmark.py \
  --mode both \
  --plugin-profile aggregates \
  --user sysdba \
  --password 'YOUR_PASSWORD' \
  --client-count 64 \
  --procedure-count 1000 \
  --target-runtime-sec 150 \
  --iterations 5
```

Сравнение новой фичи с полными SQL-текстами:

```bash
python3 bench/run_benchmark.py \
  --mode both \
  --plugin-profile sql_text \
  --user sysdba \
  --password 'YOUR_PASSWORD' \
  --client-count 64 \
  --procedure-count 1000 \
  --target-runtime-sec 150 \
  --iterations 5
```

Для более честного сравнения рекомендуется оставаться в диапазоне `3-5` итераций:
harness будет чередовать порядок режимов между итерациями, чтобы уменьшить влияние прогретого кэша.

Если нужно только сгенерировать SQL-артефакты без запуска:

```bash
python3 bench/generate_sql.py \
  --output-dir /tmp/firebird_bench_sql \
  --database-path 'localhost/3050:/tmp/firebird_bench.fdb' \
  --client-count 64 \
  --procedure-count 1000 \
  --rounds-per-client 50
```

## Что сравнивается

Основная метрика:

- время выполнения самой SQL-нагрузки в Firebird

Дополнительная метрика:

- время полного пути `SQL -> flush spool -> ingest-once -> SQLite`
- размер `spool` после flush
- размер итоговой `SQLite` после ingest

Для режима `with_plugin` harness дополнительно проверяет:

- что spool-файлы реально появились
- что `ingest-once` отработал без ошибки
- что суммарное число вызовов процедур в `SQLite` совпало с ожидаемым числом `EXECUTE PROCEDURE`
- в профиле `sql_text` - что в `sql_text_catalog` и `sql_text_usage_stats` действительно появились строки

## Как смотреть результаты

В консоль выводятся:

- `Avg SQL (s)` и `Min SQL (s)` - время самой Firebird-нагрузки
- `Avg Ingest (s)` - среднее время `ingest-once`
- `Avg Spool KB` - средний суммарный размер spool-файлов после flush
- `Avg SQLite KB` - средний размер SQLite-файла после ingest
- `Valid/Total` - сколько итераций прошло встроенные проверки

Полный JSON-отчёт лежит в:

- `bench/results/latest.json`
- `bench/results/<timestamp>.json`

В JSON есть полезные поля:

- `plugin_profile`
- `plugin_settings.enable_sql_text_stats`
- `results.<mode>.iterations[].spool_size_bytes`
- `results.<mode>.iterations[].sqlite_db_size_bytes`
- `results.<mode>.iterations[].observed_sql_text_fingerprints`
- `results.<mode>.iterations[].observed_sql_text_hour_rows`
