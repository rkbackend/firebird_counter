#include "proc_usage/collector.hpp"

#include <utility>

namespace proc_usage {

std::size_t ProcedureKeyHash::operator()(const ProcedureKey& key) const noexcept
{
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
    if (procedure_name.empty() || !should_track_database(database_path)) {
        return;
    }

    {
        // The hot path only touches memory under a short lock so normal query execution
        // does not pay the price of filesystem I/O or JSON serialization.
        std::lock_guard<std::mutex> guard(mutex_);
        counters_[ProcedureKey{database_path, procedure_name}] += 1;
    }

    flush_if_due(now);
}

bool UsageCollector::flush_if_due(std::chrono::system_clock::time_point now)
{
    {
        std::lock_guard<std::mutex> guard(mutex_);
        if (now - last_flush_at_ < config_.flush_interval) {
            return false;
        }
    }

    return flush_now(now);
}

bool UsageCollector::flush_now(std::chrono::system_clock::time_point now)
{
    CounterMap snapshot;

    {
        // We swap the whole counter map out under the lock and immediately release it.
        // This keeps the critical section short even when the snapshot contains many rows.
        std::lock_guard<std::mutex> guard(mutex_);
        if (counters_.empty()) {
            last_flush_at_ = now;
            return false;
        }

        snapshot = detach_pending_counters_unlocked();
        last_flush_at_ = now;
    }

    const auto records = build_records(snapshot, now);
    if (records.empty()) {
        return false;
    }

    if (spool_writer_ && spool_writer_->write_records(records)) {
        return true;
    }

    {
        // If the flush fails, we merge the snapshot back so the counts are not lost.
        std::lock_guard<std::mutex> guard(mutex_);
        for (const auto& [key, delta] : snapshot) {
            counters_[key] += delta;
        }
    }

    return false;
}

std::size_t UsageCollector::pending_entry_count() const
{
    std::lock_guard<std::mutex> guard(mutex_);
    return counters_.size();
}

std::uint64_t UsageCollector::pending_total_calls() const
{
    std::lock_guard<std::mutex> guard(mutex_);

    std::uint64_t total = 0;
    for (const auto& [_, count] : counters_) {
        total += count;
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

UsageCollector::CounterMap UsageCollector::detach_pending_counters_unlocked()
{
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

