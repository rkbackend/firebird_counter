#include "proc_usage/collector.hpp"

#include <utility>

namespace proc_usage {

std::size_t ProcedureKeyHash::operator()(const ProcedureKey& key) const noexcept
{
    // Хешируем обе части составного ключа и смешиваем их.
    // Благодаря этому unordered_map распределяет записи и по базе, и по процедуре.
    const std::size_t database_hash = std::hash<std::string>{}(key.database);
    const std::size_t procedure_hash = std::hash<std::string>{}(key.procedure);
    return database_hash ^ (procedure_hash << 1U);
}

UsageCollector::UsageCollector(CollectorConfig config, std::shared_ptr<SpoolWriter> spool_writer)
    : config_(std::move(config)),
      spool_writer_(std::move(spool_writer)),
      last_flush_at_(std::chrono::system_clock::now())
{
}

void UsageCollector::record_call(
    const std::string& database_path,
    const std::string& procedure_name,
    std::chrono::system_clock::time_point now
)
{
    // Сразу отбрасываем неподходящие входные данные:
    // - пустое имя процедуры нельзя корректно учитывать
    // - базы, попавшие под фильтр, специально исключены конфигом
    if (procedure_name.empty() || !should_track_database(database_path)) {
        return;
    }

    {
        // Самый частый путь выполнения затрагивает только память и держит
        // блокировку совсем недолго, чтобы обычные запросы не ждали запись
        // на диск и сериализацию JSON.
        std::lock_guard<std::mutex> guard(mutex_);
        counters_[ProcedureKey{database_path, procedure_name}] += 1;
    }

    // На каждом колбэке мы не делаем полный сброс синхронно.
    // Здесь только проверяется, прошёл ли нужный интервал.
    flush_if_due(now);
}

bool UsageCollector::flush_if_due(std::chrono::system_clock::time_point now)
{
    {
        std::lock_guard<std::mutex> guard(mutex_);
        // Ещё рано: продолжаем копить данные в памяти и быстро выходим.
        if (now - last_flush_at_ < config_.flush_interval) {
            return false;
        }
    }

    // Сам сброс выполняется уже вне первой короткой критической секции.
    return flush_now(now);
}

bool UsageCollector::flush_now(std::chrono::system_clock::time_point now)
{
    // Здесь будет временно храниться снимок всех накопленных счётчиков,
    // пока мы их записываем.
    CounterMap snapshot;

    {
        // Под блокировкой мы целиком меняем текущую таблицу счётчиков на пустую
        // и сразу отпускаем mutex.
        // Так критическая секция остаётся короткой даже при большом числе записей.
        std::lock_guard<std::mutex> guard(mutex_);
        if (counters_.empty()) {
            // Даже если работы нет, запоминаем, что момент "now" уже проверяли.
            last_flush_at_ = now;
            return false;
        }

        // После этого новые колбэки будут писать в новую пустую таблицу,
        // а "snapshot" сохранит старые накопленные значения для сериализации.
        snapshot = detach_pending_counters_unlocked();
        last_flush_at_ = now;
    }

    // Преобразуем внутреннюю таблицу счётчиков в плоский список выходных записей.
    const auto records = build_records(snapshot, now);
    if (records.empty()) {
        return false;
    }

    // Реальную запись на диск отдаём выбранной реализации writer'а.
    if (spool_writer_ && spool_writer_->write_records(records)) {
        return true;
    }

    {
        // Если сброс не удался, возвращаем данные из snapshot обратно,
        // чтобы счётчики не потерялись.
        std::lock_guard<std::mutex> guard(mutex_);
        for (const auto& [key, delta] : snapshot) {
            counters_[key] += delta;
        }
    }

    return false;
}

std::size_t UsageCollector::pending_entry_count() const
{
    // Количество уникальных пар (база, процедура), которые сейчас накоплены.
    std::lock_guard<std::mutex> guard(mutex_);
    return counters_.size();
}

std::uint64_t UsageCollector::pending_total_calls() const
{
    std::lock_guard<std::mutex> guard(mutex_);

    // Складываем все накопленные значения по всем ключам процедур.
    std::uint64_t total = 0;
    for (const auto& [_, count] : counters_) {
        total += count;
    }

    return total;
}

bool UsageCollector::should_track_database(const std::string& database_path) const
{
    // Все правила include/exclude вынесены в config.cpp,
    // чтобы сам коллектор отвечал только за подсчёт и сброс.
    return database_matches_filters(
        database_path,
        config_.include_databases,
        config_.exclude_databases
    );
}

UsageCollector::CounterMap UsageCollector::detach_pending_counters_unlocked()
{
    // swap() для самого объекта map работает за O(1):
    // мы передаём владение всей таблицей сразу, а не копируем записи по одной.
    CounterMap detached;
    detached.swap(counters_);
    return detached;
}

std::vector<FlushRecord> UsageCollector::build_records(
    const CounterMap& counters,
    std::chrono::system_clock::time_point timestamp
) const
{
    std::vector<FlushRecord> records;
    records.reserve(counters.size());

    for (const auto& [key, delta] : counters) {
        // Каждый накопленный счётчик превращается в одну отдельную выходную запись.
        records.push_back(FlushRecord{
            .timestamp = timestamp,
            .database = key.database,
            .procedure = key.procedure,
            .delta = delta,
        });
    }

    return records;
}

}  // namespace proc_usage
