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

struct ProcedureKey {
    std::string database;
    std::string procedure;

    bool operator==(const ProcedureKey& other) const = default;
};

struct ProcedureKeyHash {
    std::size_t operator()(const ProcedureKey& key) const noexcept;
};

struct FlushRecord {
    std::chrono::system_clock::time_point timestamp;
    std::string database;
    std::string procedure;
    std::uint64_t delta;
};

class SpoolWriter {
public:
    virtual ~SpoolWriter() = default;
    virtual bool write_records(const std::vector<FlushRecord>& records) = 0;
};

class UsageCollector {
public:
    UsageCollector(CollectorConfig config, std::shared_ptr<SpoolWriter> spool_writer);

    // Records one observed procedure call. The collector only increments in-memory counters here.
    void record_call(
        const std::string& database_path,
        const std::string& procedure_name,
        std::chrono::system_clock::time_point now = std::chrono::system_clock::now()
    );

    // Flushes only when the configured interval has elapsed.
    bool flush_if_due(std::chrono::system_clock::time_point now = std::chrono::system_clock::now());

    // Flushes immediately, even if the interval has not elapsed yet.
    bool flush_now(std::chrono::system_clock::time_point now = std::chrono::system_clock::now());

    std::size_t pending_entry_count() const;
    std::uint64_t pending_total_calls() const;

private:
    using CounterMap = std::unordered_map<ProcedureKey, std::uint64_t, ProcedureKeyHash>;

    bool should_track_database(const std::string& database_path) const;

    // Moves the current counters out of the lock so disk I/O does not block new events.
    CounterMap detach_pending_counters_unlocked();

    std::vector<FlushRecord> build_records(
        const CounterMap& counters,
        std::chrono::system_clock::time_point timestamp
    ) const;

    CollectorConfig config_;
    std::shared_ptr<SpoolWriter> spool_writer_;

    mutable std::mutex mutex_;
    CounterMap counters_;
    std::chrono::system_clock::time_point last_flush_at_;
};

}  // namespace proc_usage

