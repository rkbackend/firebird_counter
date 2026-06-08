# Счетчик Использования Процедур Firebird

В этом репозитории лежит гибридная реализация для подсчета использования хранимых процедур в Firebird 3 с минимальной нагрузкой на рабочую систему:

- `C++`-ядро сборщика, которое работает рядом с trace-callback'ами Firebird, держит счетчики в памяти и периодически сбрасывает компактные `JSONL`-снимки.
- `Python`-сервис, который забирает эти снимки, сохраняет агрегированную статистику в `SQLite` и предоставляет небольшой CLI.

## Структура Репозитория

- `cpp/` - ядро сборщика, парсер конфигурации, writer для spool-файлов и каркас Firebird bridge
- `proc_usage/` - Python-сервис, storage, ingestion и CLI
- `configs/` - примеры конфигов для Firebird trace и самого сборщика
- `tests/` - тесты на `unittest`, которые прогоняют Python-цепочку end-to-end

## Текущее Состояние

Python-часть готова к запуску и работает только на стандартной библиотеке.
Минимальная поддерживаемая версия Python для collector/CLI: `3.9`.

`C++`-ядро сборщика реализовано отдельно от Firebird SDK, поэтому его проще читать и тестировать само по себе. Специфичная для Firebird точка входа собирается через отдельную build-опцию, потому что для нее нужны заголовки SDK Firebird и `C++`-компилятор.

## Использование Python-Части

Создайте конфиг, например `configs/python_service.json`:

```json
{
  "spool_dir": "/tmp/firebird_proc_usage_spool",
  "sqlite_db_path": "./var/proc_usage.sqlite3",
  "poll_interval_sec": 5
}
```

Инициализируйте базу:

```bash
python3 -m proc_usage init-db --config configs/python_service.json
```

Один раз обработайте накопившиеся snapshot-файлы:

```bash
python3 -m proc_usage ingest-once --config configs/python_service.json
```

Запустите долгоживущий сервис:

```bash
python3 -m proc_usage serve --config configs/python_service.json
```

Покажите самые часто вызываемые процедуры:

```bash
python3 -m proc_usage top --config configs/python_service.json --limit 10
```

Покажите статистику по одной процедуре:

```bash
python3 -m proc_usage show MY_PROC --config configs/python_service.json
```

## Сборка `C++`-Части

В проекте есть `CMakeLists.txt`, который собирает:

- `proc_usage_core` - библиотеку с ядром сборщика
- `proc_usage_firebird_plugin` - опциональную динамическую библиотеку, только если доступны заголовки Firebird SDK и включен `PROC_USAGE_ENABLE_FIREBIRD_SDK=ON`

Пример:

```bash
cmake -S . -B build -DPROC_USAGE_ENABLE_FIREBIRD_SDK=ON -DFIREBIRD_INCLUDE_DIR=/usr/include/firebird
cmake --build build
```

## RPM / `yum` Установка

В репозитории есть готовая RPM-упаковка:

- spec-файл: [packaging/rpm/firebird-proc-usage.spec](/home/j8r/code/firebird_counter/packaging/rpm/firebird-proc-usage.spec)
- systemd unit: [packaging/rpm/proc-usage.service](/home/j8r/code/firebird_counter/packaging/rpm/proc-usage.service)
- production-конфиг collector: [packaging/rpm/python_service.json](/home/j8r/code/firebird_counter/packaging/rpm/python_service.json)
- production-конфиг plugin: [packaging/rpm/proc_usage_plugin.conf](/home/j8r/code/firebird_counter/packaging/rpm/proc_usage_plugin.conf)

Пакет собирается в двух частях:

- `firebird-proc-usage` - Python collector, CLI, конфиги и `systemd` unit
- `firebird-proc-usage-firebird-plugin` - `.so` trace plugin для Firebird

Типовой порядок на `RHEL`/`CentOS`/`AlmaLinux`/`Rocky`:

```bash
yum install -y rpm-build gcc-c++ cmake make \
  python3 python3-devel python3-setuptools python3-build python3-installer \
  pyproject-rpm-macros systemd-rpm-macros firebird-devel
```

Подготовьте source tarball, чтобы корневой каталог внутри архива назывался `firebird-proc-usage-0.1.0/`, затем соберите RPM:

```bash
rpmbuild -bb packaging/rpm/firebird-proc-usage.spec --with firebird_plugin
```

Если нужен только Python collector без Firebird SDK:

```bash
rpmbuild -bb packaging/rpm/firebird-proc-usage.spec --without firebird_plugin
```

После сборки установка выглядит обычно:

```bash
yum install -y ~/rpmbuild/RPMS/*/firebird-proc-usage-0.1.0-1*.rpm
yum install -y ~/rpmbuild/RPMS/*/firebird-proc-usage-firebird-plugin-0.1.0-1*.rpm
```

Что ставится по умолчанию:

- collector config: `/etc/firebird-proc-usage/python_service.json`
- plugin config: `/etc/firebird-proc-usage/firebird/proc_usage_plugin.conf`
- spool и SQLite: `/var/lib/firebird-proc-usage/`
- systemd service: `proc-usage.service`

После установки останется:

1. Подключить `libproc_usage_trace.so` в Firebird `plugins.conf`
2. При необходимости скорректировать каталог plugin'ов через macro `%{_libdir}/firebird/plugins` в spec-файле под вашу сборку Firebird
3. Включить collector: `systemctl enable --now proc-usage.service`

## Заметки По Интеграции С Firebird

Динамическая библиотека собирается в:

- `build-firebird/libproc_usage_trace.so`

Плагин регистрируется внутри библиотеки под именем `ProcUsageTrace`.

Ключевая точка интеграции - класс `proc_usage::firebird::FirebirdTraceBridge`. Trace plugin Firebird передает событие `trace_proc_execute(started=true)` в ядро сборщика, не добавляя запись в БД прямо в request path.

Конфигурацию сборщика можно передать двумя способами:

1. Предпочтительный способ для локального тестирования: задать `PROC_USAGE_PLUGIN_CONFIG` и указать простой config-файл формата `key = value`
2. Более удобный для production способ: настроить плагин в `plugins.conf`; Firebird передаст эти значения через `IPluginConfig`

Примеры конфигов лежат в [configs](./configs).

## Локальный Тест Firebird 3

1. Соберите trace plugin:

```bash
cmake -S . -B build-firebird -DPROC_USAGE_ENABLE_FIREBIRD_SDK=ON -DFIREBIRD_INCLUDE_DIR=/usr/include/firebird
cmake --build build-firebird
```

2. Скопируйте библиотеку плагина в каталог `plugins` вашего Firebird.
   Точный путь зависит от установки, часто это `/usr/lib/firebird/3.0/plugins/` или `/opt/firebird/plugins/`.

3. Положите конфиг сборщика в место, доступное для чтения процессу Firebird, например:

```ini
spool_dir = /tmp/firebird_proc_usage_spool
debug_log_path = /tmp/proc_usage_trace_debug.log
flush_interval_sec = 5
include_databases =
exclude_databases =
```

4. Передайте путь к конфигу через окружение сервиса Firebird или настройте те же ключи в `plugins.conf`:

```bash
export PROC_USAGE_PLUGIN_CONFIG=/absolute/path/to/proc_usage_plugin.conf
```

5. Настройте Firebird на использование плагина:

- См. [configs/plugins.conf.sample](./configs/plugins.conf.sample)
- См. [configs/firebird.conf.sample](./configs/firebird.conf.sample)
- См. [configs/fbtrace.conf.sample](./configs/fbtrace.conf.sample)

6. Перезапустите Firebird.

7. Создайте небольшую тестовую БД и процедуру:

```sql
CREATE DATABASE '/tmp/proc_usage_demo.fdb';

SET TERM ^;
CREATE OR ALTER PROCEDURE PROC_A
AS
BEGIN
END^
SET TERM ;^

EXECUTE PROCEDURE PROC_A;
EXECUTE PROCEDURE PROC_A;
COMMIT;
```

8. Подождите хотя бы один `flush_interval`, затем обработайте spool:

```bash
python3 -m proc_usage init-db --config configs/python_service.json
python3 -m proc_usage ingest-once --config configs/python_service.json
python3 -m proc_usage top --config configs/python_service.json --limit 10
```

Если Firebird-часть подключена правильно, вы увидите `PROC_A` в статистике `SQLite`.

## Локальный Benchmark

Для нагрузочного сравнения `without_plugin` и `with_plugin` используйте harness из [bench](./bench):

```bash
python3 bench/run_benchmark.py \
  --mode both \
  --user sysdba \
  --password 'YOUR_PASSWORD' \
  --client-count 64 \
  --procedure-count 1000 \
  --target-runtime-sec 20 \
  --iterations 3 \
  --sudo-command 'sudo -S'
```

Что делает benchmark:

- создает отдельную benchmark-БД на Firebird
- генерирует `1000` процедур `BENCH_PROC_0001 .. BENCH_PROC_1000`
- запускает `64` параллельных клиента `isql-fb`
- смешивает `EXECUTE PROCEDURE`, `SELECT`, `UPDATE`, `INSERT` и `DELETE`
- прогоняет нагрузку сначала без `ProcUsageTrace`, затем с `ProcUsageTrace`
- сохраняет JSON-отчет в `bench/results/` или, если каталог недоступен на запись, в `workspace/results/`

В этой установке Firebird локальный benchmark использует TCP-подключение вида:

```text
localhost/3050:/tmp/firebird_benchmark_<run_id>.fdb
```

Это обходит ограничения локального сокета `/tmp/firebird` и гарантирует, что нагрузка идет через сервер Firebird, а не через embedded engine.

## Общий Spool-Каталог

Когда Firebird пишет spool-файлы от имени системного пользователя `firebird`, collector, запущенный от другого пользователя, может не суметь переименовать файлы внутри spool-каталога.

Сейчас Python-collector безопасно умеет с этим работать:

- сначала он пробует обычное переименование `.jsonl -> .processing`
- если переименование запрещено, он обрабатывает файл прямо на месте
- он делает дедупликацию по fingerprint файла в `SQLite`, поэтому повторные сканирования не удваивают счетчики

Для локальных Firebird-установок в этом репозитории рекомендуемый spool-каталог:
`/tmp/firebird_proc_usage_spool`.

Если нужно диагностировать Firebird-часть, включите `debug_log_path` в конфиге плагина и смотрите `/tmp/proc_usage_trace_debug.log`. Плагин пишет туда создание trace factory, вызовы процедур и результаты flush.
