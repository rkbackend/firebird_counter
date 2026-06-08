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

// Ключ, который используется во внутренней таблице счётчиков.
// Вызовы считаются отдельно для каждой пары:
// 1. путь к базе данных
// 2. имя процедуры
struct ProcedureKey {
    std::string database;
    std::string procedure;

    bool operator==(const ProcedureKey& other) const = default;
};

// Пользовательский хеш для ProcedureKey, чтобы ключ можно было хранить
// в std::unordered_map.
// Точная формула не важна для бизнес-логики: мы просто объединяем
// хеш пути к базе с хешем имени процедуры.
struct ProcedureKeyHash {
    std::size_t operator()(const ProcedureKey& key) const noexcept;
};

// Одна запись, уже готовая к сбросу на диск.
// "delta" означает, сколько новых вызовов накопилось с прошлого сброса.
struct FlushRecord {
    std::chrono::system_clock::time_point timestamp;
    std::string database;
    std::string procedure;
    std::uint64_t delta;
};

// Абстрактная точка вывода для накопленных записей.
// Сейчас данные пишутся в JSONL-файлы, но сам коллектор не должен знать,
// куда именно они отправляются, пока есть реализация этого интерфейса.
class SpoolWriter {
public:
    virtual ~SpoolWriter() = default;
    virtual bool write_records(const std::vector<FlushRecord>& records) = 0;
};

// Центральный накопитель счётчиков в памяти.
// Firebird вызывает record_call() много раз. Коллектор увеличивает счётчики
// в RAM и только иногда сбрасывает снимок накопленных данных на диск.
class UsageCollector {
public:
    UsageCollector(CollectorConfig config, std::shared_ptr<SpoolWriter> spool_writer);

    // Регистрирует один замеченный вызов процедуры.
    // Здесь коллектор только увеличивает счётчики в памяти.
    void record_call(
        const std::string& database_path,
        const std::string& procedure_name,
        std::chrono::system_clock::time_point now = std::chrono::system_clock::now()
    );

    // Выполняет сброс только если прошёл настроенный интервал.
    bool flush_if_due(std::chrono::system_clock::time_point now = std::chrono::system_clock::now());

    // Выполняет сброс сразу, даже если интервал ещё не прошёл.
    bool flush_now(std::chrono::system_clock::time_point now = std::chrono::system_clock::now());

    std::size_t pending_entry_count() const;
    std::uint64_t pending_total_calls() const;

private:
    // Таблица вида:
    //   (база, процедура) -> сколько вызовов уже замечено, но ещё не записано на диск
    using CounterMap = std::unordered_map<ProcedureKey, std::uint64_t, ProcedureKeyHash>;

    bool should_track_database(const std::string& database_path) const;

    // Выносит текущие счётчики из-под блокировки, чтобы операции записи на диск
    // не мешали приёму новых событий.
    CounterMap detach_pending_counters_unlocked();

    std::vector<FlushRecord> build_records(
        const CounterMap& counters,
        std::chrono::system_clock::time_point timestamp
    ) const;

    CollectorConfig config_;
    std::shared_ptr<SpoolWriter> spool_writer_;

    // Защищает и counters_, и last_flush_at_, потому что к ним одновременно
    // обращаются рабочие потоки Firebird.
    mutable std::mutex mutex_;
    CounterMap counters_;
    // Время последней успешной попытки принять решение о сбросе.
    // Обновляется и когда данные реально сброшены, и когда мы увидели,
    // что сбрасывать нечего, чтобы не проверять это на каждом колбэке снова.
    std::chrono::system_clock::time_point last_flush_at_;
};

}  // namespace proc_usage
