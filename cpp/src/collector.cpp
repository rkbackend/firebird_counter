#include "proc_usage/collector.hpp"

#include <algorithm>
#include <iomanip>
#include <sstream>
#include <utility>

namespace proc_usage {
namespace {

std::string format_usage_hour(std::chrono::system_clock::time_point timestamp)
{
    // Все события группируются по часу в UTC, чтобы статистика не зависела
    // от локальной таймзоны конкретного сервера.
    const std::time_t raw_time = std::chrono::system_clock::to_time_t(timestamp);
    std::tm utc_time {};
#if defined(_WIN32)
    gmtime_s(&utc_time, &raw_time);
#else
    gmtime_r(&raw_time, &utc_time);
#endif

    std::ostringstream output;
    output << std::put_time(&utc_time, "%Y-%m-%dT%H:00Z");
    return output.str();
}

}  // namespace

std::size_t UsageKeyHash::operator()(const UsageKey& key) const noexcept
{
    const std::size_t kind_hash = std::hash<int>{}(static_cast<int>(key.kind));
    const std::size_t hour_hash = std::hash<std::string>{}(key.usage_hour);
    const std::size_t database_hash = std::hash<std::string>{}(key.database);
    const std::size_t name_hash = std::hash<std::string>{}(key.name);
    return kind_hash ^ (hour_hash << 1U) ^ (database_hash << 2U) ^ (name_hash << 3U);
}

void UsageAggregate::observe(
    std::uint64_t duration_ms,
    std::chrono::system_clock::time_point observed_at
)
{
    // Новое событие сразу вливается в уже накопленные агрегаты.
    total_time_ms += duration_ms;
    min_time_ms = count == 0 ? duration_ms : std::min(min_time_ms, duration_ms);
    max_time_ms = count == 0 ? duration_ms : std::max(max_time_ms, duration_ms);
    last_seen_at = count == 0 ? observed_at : std::max(last_seen_at, observed_at);
    count += 1;
}

UsageCollector::UsageCollector(CollectorConfig config, std::shared_ptr<SpoolWriter> spool_writer)
    : config_(std::move(config)),
      spool_writer_(std::move(spool_writer)),
      last_flush_at_(std::chrono::system_clock::now())
{
}

void UsageCollector::record_usage(
    UsageKind kind,
    const std::string& database_path,
    const std::string& name,
    std::uint64_t duration_ms,
    std::chrono::system_clock::time_point now
)
{
    // Пустые имена и базы, не прошедшие фильтры include/exclude,
    // не должны попадать в статистику.
    if (name.empty() || !should_track_database(database_path)) {
        return;
    }

    const std::string usage_hour = format_usage_hour(now);

    {
        // Под mutex только быстрое обновление in-memory структуры.
        std::lock_guard<std::mutex> guard(mutex_);
        aggregates_[UsageKey{kind, usage_hour, database_path, name}].observe(duration_ms, now);
    }

    // Проверку времени flush делаем уже вне критической секции.
    flush_if_due(now);
}

bool UsageCollector::flush_if_due(std::chrono::system_clock::time_point now)
{
    {
        std::lock_guard<std::mutex> guard(mutex_);
        // Пока не истёк интервал, просто продолжаем копить данные в памяти.
        if (now - last_flush_at_ < config_.flush_interval) {
            return false;
        }
    }

    return flush_now(now);
}

bool UsageCollector::flush_now(std::chrono::system_clock::time_point now)
{
    AggregateMap snapshot;

    {
        std::lock_guard<std::mutex> guard(mutex_);
        // Даже если данных нет, обновляем last_flush_at_, чтобы не пытаться
        // бесконечно выполнять пустой flush на каждом следующем событии.
        if (aggregates_.empty()) {
            last_flush_at_ = now;
            return false;
        }

        // Забираем весь текущий снимок и сразу освобождаем основной контейнер.
        // Это позволяет новым trace-событиям копиться параллельно с записью на диск.
        snapshot = detach_pending_aggregates_unlocked();
        last_flush_at_ = now;
    }

    const auto records = build_records(snapshot);
    if (records.empty()) {
        return false;
    }

    if (spool_writer_ && spool_writer_->write_records(records)) {
        // Пакет успешно записан во внешний spool, больше делать ничего не нужно.
        return true;
    }

    {
        std::lock_guard<std::mutex> guard(mutex_);
        // Если запись не удалась, возвращаем snapshot обратно.
        // Так мы не теряем статистику и сможем повторить flush позже.
        for (const auto& [key, aggregate] : snapshot) {
            auto& current = aggregates_[key];
            if (current.count == 0) {
                current = aggregate;
                continue;
            }

            current.total_time_ms += aggregate.total_time_ms;
            current.min_time_ms = std::min(current.min_time_ms, aggregate.min_time_ms);
            current.max_time_ms = std::max(current.max_time_ms, aggregate.max_time_ms);
            current.last_seen_at = std::max(current.last_seen_at, aggregate.last_seen_at);
            current.count += aggregate.count;
        }
    }

    return false;
}

std::size_t UsageCollector::pending_entry_count() const
{
    // Это число уникальных агрегатных ключей, а не количество вызовов.
    std::lock_guard<std::mutex> guard(mutex_);
    return aggregates_.size();
}

std::uint64_t UsageCollector::pending_total_calls() const
{
    std::lock_guard<std::mutex> guard(mutex_);

    std::uint64_t total = 0;
    for (const auto& [_, aggregate] : aggregates_) {
        // Здесь суммируется count каждого агрегата, чтобы понять общий объём.
        total += aggregate.count;
    }

    return total;
}

bool UsageCollector::should_track_database(const std::string& database_path) const
{
    return database_matches_filters(
        database_path,
        config_.include_databases,
        config_.exclude_databases
    );
}

UsageCollector::AggregateMap UsageCollector::detach_pending_aggregates_unlocked()
{
    // swap позволяет вынуть весь контейнер почти бесплатно, без копирования.
    AggregateMap detached;
    detached.swap(aggregates_);
    return detached;
}

std::vector<FlushRecord> UsageCollector::build_records(const AggregateMap& aggregates) const
{
    // Преобразуем hash-map в плоский список, удобный для JSONL-сериализации.
    std::vector<FlushRecord> records;
    records.reserve(aggregates.size());

    for (const auto& [key, aggregate] : aggregates) {
        records.push_back(FlushRecord{
            .timestamp = aggregate.last_seen_at,
            .kind = key.kind,
            .usage_hour = key.usage_hour,
            .database = key.database,
            .name = key.name,
            .count = aggregate.count,
            .total_time_ms = aggregate.total_time_ms,
            .min_time_ms = aggregate.min_time_ms,
            .max_time_ms = aggregate.max_time_ms,
        });
    }

    return records;
}

}  // namespace proc_usage
