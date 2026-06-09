#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#include "proc_usage/config.hpp"

namespace proc_usage {

enum class UsageKind {
    procedure,
    sql,
};

// Ключ внутренней таблицы агрегатов.
// Статистика считается отдельно для типа сущности, базы и имени.
struct UsageKey {
    UsageKind kind;
    std::string usage_hour;
    std::string database;
    std::string name;

    bool operator==(const UsageKey& other) const = default;
};

struct UsageKeyHash {
    std::size_t operator()(const UsageKey& key) const noexcept;
};

// Накопленные метрики для одного ключа.
struct UsageAggregate {
    std::uint64_t count {0};
    std::uint64_t total_time_ms {0};
    std::uint64_t min_time_ms {0};
    std::uint64_t max_time_ms {0};
    std::chrono::system_clock::time_point last_seen_at {};

    void observe(
        std::uint64_t duration_ms,
        std::chrono::system_clock::time_point observed_at
    );
};

// Одна агрегированная запись, готовая к сбросу на диск.
struct FlushRecord {
    std::chrono::system_clock::time_point timestamp;
    UsageKind kind;
    std::string usage_hour;
    std::string database;
    std::string name;
    std::uint64_t count;
    std::uint64_t total_time_ms;
    std::uint64_t min_time_ms;
    std::uint64_t max_time_ms;
};

class SpoolWriter {
public:
    virtual ~SpoolWriter() = default;
    virtual bool write_records(const std::vector<FlushRecord>& records) = 0;
};

// Центральный накопитель агрегированной статистики в памяти.
class UsageCollector {
public:
    UsageCollector(CollectorConfig config, std::shared_ptr<SpoolWriter> spool_writer);

    // Регистрирует одно завершённое выполнение процедуры или SQL-запроса.
    void record_usage(
        UsageKind kind,
        const std::string& database_path,
        const std::string& name,
        std::uint64_t duration_ms,
        std::chrono::system_clock::time_point now = std::chrono::system_clock::now()
    );

    bool flush_if_due(std::chrono::system_clock::time_point now = std::chrono::system_clock::now());
    bool flush_now(std::chrono::system_clock::time_point now = std::chrono::system_clock::now());

    std::size_t pending_entry_count() const;
    std::uint64_t pending_total_calls() const;

private:
    using AggregateMap = std::unordered_map<UsageKey, UsageAggregate, UsageKeyHash>;

    bool should_track_database(const std::string& database_path) const;
    AggregateMap detach_pending_aggregates_unlocked();
    std::vector<FlushRecord> build_records(const AggregateMap& aggregates) const;

    CollectorConfig config_;
    std::shared_ptr<SpoolWriter> spool_writer_;

    mutable std::mutex mutex_;
    AggregateMap aggregates_;
    std::chrono::system_clock::time_point last_flush_at_;
};

}  // namespace proc_usage
