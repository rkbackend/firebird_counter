# Отчет По Fair Benchmark ProcUsageTrace

Дата: 2026-06-09

## 1. Цель Прогона

Целью этого прогона было более честно сравнить `without_plugin` и `with_plugin`
после добавления timing-агрегации `min/max/avg/count`.

В отличие от раннего сценария, здесь benchmark:

- чередует порядок режимов между итерациями
- делает несколько повторов на каждый режим
- тем самым уменьшает перекос от прогретого кэша и порядка запуска

## 2. Параметры Прогона

Команда запуска:

```bash
python3 bench/run_benchmark.py \
  --mode both \
  --workspace-root /tmp/firebird_proc_usage_benchmark_fair_run1 \
  --client-count 4 \
  --procedure-count 50 \
  --target-runtime-sec 10 \
  --calibration-rounds 2 \
  --min-rounds-per-client 2 \
  --iterations 5 \
  --user sysdba \
  --password '***' \
  --sudo-command 'sudo -S'
```

Основные параметры:

- `4` клиента
- `50` процедур
- `5` итераций на каждый режим
- целевая длительность: `10s`
- фактически подобрано: `16` раундов на клиента

## 3. Execution Plan

Порядок режимов чередовался по итерациям:

1. `without_plugin -> with_plugin`
2. `with_plugin -> without_plugin`
3. `without_plugin -> with_plugin`
4. `with_plugin -> without_plugin`
5. `without_plugin -> with_plugin`

## 4. Результаты

### Без плагина

- среднее SQL-время: `1.260s`
- минимальное SQL-время: `1.175s`
- валидных итераций: `5/5`

Итерации:

- `1`: `1.348s`
- `2`: `1.176s`
- `3`: `1.175s`
- `4`: `1.260s`
- `5`: `1.343s`

### С плагином

- среднее SQL-время: `1.238s`
- минимальное SQL-время: `1.183s`
- среднее время `ingest-once`: `0.063s`
- валидных итераций: `5/5`

Итерации:

- `1`: SQL `1.183s`, ingest `0.063s`
- `2`: SQL `1.358s`, ingest `0.064s`
- `3`: SQL `1.188s`, ingest `0.062s`
- `4`: SQL `1.272s`, ingest `0.065s`
- `5`: SQL `1.188s`, ingest `0.062s`

## 5. Проверка Корректности

Для каждой plugin-итерации было ожидаемо:

- `448` вызовов `EXECUTE PROCEDURE`

Фактически benchmark подтвердил:

- `observed_execute_procedure_calls = 448`
- `distinct_procedures_observed = 50`
- spool-файлы создавались стабильно
- `ingest-once` проходил успешно

## 6. Вывод

В этом fair-сценарии:

- все итерации baseline валидны: `5/5`
- все итерации plugin-режима валидны: `5/5`
- SQL overhead относительно baseline не проявился
- формально получилось `-0.023s (-1.79%)`

Такой отрицательный overhead не стоит трактовать как "плагин ускоряет Firebird".
Практически это означает, что на данном масштабе нагрузки влияние плагина
на SQL-path находится в пределах шума измерения, а сам `ingest` остаётся быстрым.

## 7. Артефакты

JSON-отчёт:

- `/tmp/firebird_proc_usage_benchmark_fair_run1/results/latest.json`

SQLite benchmark storage:

- `/tmp/firebird_proc_usage_benchmark_fair_run1/proc_usage_benchmark.sqlite3`
