# Benchmark Report: `aggregates` vs `sql_text`

Дата прогона: `2026-06-09`

## Контекст

Цель: сравнить нагрузку Firebird benchmark в двух профилях плагина:

- `aggregates` - процедуры + агрегаты по `sql_kind`
- `sql_text` - процедуры + агрегаты по `sql_kind` + полные SQL-тексты

Перед прогоном:

- обновлён системный plugin-файл `libproc_usage_trace.so`
- для benchmark использовался отдельный trace-config: `/tmp/benchmark_sql_text_fbtrace.conf`
- каталог `spool` был открыт на запись для процесса Firebird: `/tmp/firebird_proc_usage_spool`

## Команды

```bash
python3 bench/run_benchmark.py \
  --mode both \
  --plugin-profile aggregates \
  --workspace-root /tmp/firebird_proc_usage_benchmark_aggregates_report \
  --client-count 4 \
  --procedure-count 50 \
  --target-runtime-sec 10 \
  --calibration-rounds 2 \
  --min-rounds-per-client 2 \
  --iterations 3 \
  --user sysdba \
  --password '***' \
  --audit-trace-conf /tmp/benchmark_sql_text_fbtrace.conf \
  --sudo-command /tmp/bench_sudo.sh
```

```bash
python3 bench/run_benchmark.py \
  --mode both \
  --plugin-profile sql_text \
  --workspace-root /tmp/firebird_proc_usage_benchmark_sql_text_report \
  --client-count 4 \
  --procedure-count 50 \
  --target-runtime-sec 10 \
  --calibration-rounds 2 \
  --min-rounds-per-client 2 \
  --iterations 3 \
  --user sysdba \
  --password '***' \
  --audit-trace-conf /tmp/benchmark_sql_text_fbtrace.conf \
  --sudo-command /tmp/bench_sudo.sh
```

## Результаты

### Профиль `aggregates`

- `rounds_per_client`: `15`
- валидность: `3/3` для `without_plugin`, `3/3` для `with_plugin`
- SQL overhead vs baseline: `-0.032s` (`-2.55%`)

| Mode | Avg SQL, s | Min SQL, s | Avg Ingest, s | Avg Spool, KB | Avg SQLite, KB |
|---|---:|---:|---:|---:|---:|
| `without_plugin` | `1.273` | `1.244` | `-` | `0.0` | `44.0` |
| `with_plugin` | `1.241` | `1.182` | `0.068` | `126.2` | `77.3` |

Наблюдения:

- `with_plugin` зафиксировал `420` вызовов `EXECUTE PROCEDURE` в каждой итерации
- `sql_text`-строки в этом профиле ожидаемо отсутствуют
- spool-файлов: `63-64` на итерацию

### Профиль `sql_text`

- `rounds_per_client`: `17`
- валидность: `3/3` для `without_plugin`, `3/3` для `with_plugin`
- SQL overhead vs baseline: `+0.082s` (`+6.81%`)

| Mode | Avg SQL, s | Min SQL, s | Avg Ingest, s | Avg Spool, KB | Avg SQLite, KB |
|---|---:|---:|---:|---:|---:|
| `without_plugin` | `1.202` | `1.172` | `-` | `0.0` | `44.0` |
| `with_plugin` | `1.284` | `1.101` | `0.084` | `322.6` | `256.0` |

Наблюдения:

- `with_plugin` зафиксировал `476` вызовов `EXECUTE PROCEDURE` в каждой итерации
- `sql_text`-каталог стабильно содержал `259` fingerprint-ов
- `sql_text_usage_stats` стабильно содержала `259` почасовых строк
- spool-файлов: `70-73` на итерацию

## Выводы

1. Новый профиль `sql_text` реально собирает полные SQL-тексты и заметно увеличивает объём данных.
2. По сравнению с `aggregates`, профиль `sql_text` увеличил:
   - средний размер `spool` примерно с `126 KB` до `323 KB`
   - итоговый размер `SQLite` примерно с `77 KB` до `256 KB`
   - среднее время `ingest` примерно с `0.068s` до `0.084s`
3. По SQL runtime в этом коротком локальном прогоне:
   - `aggregates` не показал деградации относительно baseline
   - `sql_text` дал умеренный overhead около `6.8%`

## Сырые отчёты

- [aggregates latest.json](/tmp/firebird_proc_usage_benchmark_aggregates_report/results/latest.json)
- [sql_text latest.json](/tmp/firebird_proc_usage_benchmark_sql_text_report/results/latest.json)
