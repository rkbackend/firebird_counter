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
    // Вызов хранимой процедуры.
    procedure,
    // Обычный SQL statement, попавший в trace API Firebird.
    sql,
};

// Ключ внутренней таблицы агрегатов.
// Статистика считается отдельно:
// 1. по типу сущности,
// 2. по часу в UTC,
// 3. по базе данных,
// 4. по имени процедуры или категории SQL.
//
// Такая комбинация затем соответствует одной строке в SQLite-таблице.
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
// Сырые длительности отдельных вызовов здесь не хранятся: только агрегаты,
// которые можно безопасно наращивать по мере прихода событий.
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
// Это промежуточный формат между in-memory коллекцией и JSONL spool-файлом.
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
    // Возвращает true, если пакет удалось полностью записать и опубликовать.
    virtual bool write_records(const std::vector<FlushRecord>& records) = 0;
};

// Центральный накопитель агрегированной статистики в памяти.
// Firebird-плагин отправляет сюда события завершённых операций, а коллектор:
// 1. схлопывает их по почасовому ключу;
// 2. периодически отдаёт снимок во внешний writer;
// 3. при ошибке записи возвращает данные обратно в память.
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

    // Публикует накопленные данные только если истёк flush_interval.
    bool flush_if_due(std::chrono::system_clock::time_point now = std::chrono::system_clock::now());
    // Принудительно публикует всё, что накоплено на текущий момент.
    bool flush_now(std::chrono::system_clock::time_point now = std::chrono::system_clock::now());

    // Служебные методы для диагностики, логов и тестов.
    std::size_t pending_entry_count() const;
    std::uint64_t pending_total_calls() const;

private:
    using AggregateMap = std::unordered_map<UsageKey, UsageAggregate, UsageKeyHash>;

    bool should_track_database(const std::string& database_path) const;
    // Быстро вынимает весь map агрегатов, оставляя основной контейнер пустым.
    // Вызывать только под уже захваченным mutex.
    AggregateMap detach_pending_aggregates_unlocked();
    // Преобразует hash-map агрегатов в линейный список для сериализации.
    std::vector<FlushRecord> build_records(const AggregateMap& aggregates) const;

    CollectorConfig config_;
    std::shared_ptr<SpoolWriter> spool_writer_;

    mutable std::mutex mutex_;
    AggregateMap aggregates_;
    std::chrono::system_clock::time_point last_flush_at_;
};

}  // namespace proc_usage
