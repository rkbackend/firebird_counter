#pragma once

#include <chrono>
#include <filesystem>
#include <string>
#include <vector>

namespace proc_usage {

struct CollectorConfig {
    std::chrono::seconds flush_interval {30};
    std::filesystem::path spool_dir;
    std::filesystem::path debug_log_path;
    std::vector<std::string> include_databases;
    std::vector<std::string> exclude_databases;
};

// Loads a tiny "key = value" config file for the collector.
CollectorConfig load_collector_config_from_file(const std::filesystem::path& path);

// Returns true when a database path should be tracked according to include/exclude filters.
bool database_matches_filters(
    const std::string& database_path,
    const std::vector<std::string>& include_filters,
    const std::vector<std::string>& exclude_filters
);

}  // namespace proc_usage
