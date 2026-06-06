#include "proc_usage/config.hpp"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace proc_usage {
namespace {

std::string trim(std::string value)
{
    auto not_space = [](unsigned char ch) { return !std::isspace(ch); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), not_space));
    value.erase(std::find_if(value.rbegin(), value.rend(), not_space).base(), value.end());
    return value;
}

std::vector<std::string> split_list(const std::string& value)
{
    std::vector<std::string> parts;
    std::stringstream stream(value);
    std::string part;

    while (std::getline(stream, part, ';')) {
        part = trim(part);
        if (!part.empty()) {
            parts.push_back(part);
        }
    }

    return parts;
}

}  // namespace

CollectorConfig load_collector_config_from_file(const std::filesystem::path& path)
{
    std::ifstream input(path);
    if (!input) {
        throw std::runtime_error("Unable to open collector config: " + path.string());
    }

    CollectorConfig config;
    std::string line;
    std::size_t line_number = 0;

    while (std::getline(input, line)) {
        ++line_number;
        line = trim(line);

        if (line.empty() || line.starts_with('#')) {
            continue;
        }

        const auto separator = line.find('=');
        if (separator == std::string::npos) {
            throw std::runtime_error("Invalid config line " + std::to_string(line_number) + ": " + line);
        }

        const std::string key = trim(line.substr(0, separator));
        const std::string value = trim(line.substr(separator + 1));

        if (key == "spool_dir") {
            config.spool_dir = value;
        }
        else if (key == "debug_log_path") {
            config.debug_log_path = value;
        }
        else if (key == "flush_interval_sec") {
            config.flush_interval = std::chrono::seconds(std::stoll(value));
        }
        else if (key == "include_databases") {
            config.include_databases = split_list(value);
        }
        else if (key == "exclude_databases") {
            config.exclude_databases = split_list(value);
        }
        else {
            throw std::runtime_error("Unknown config key: " + key);
        }
    }

    if (config.spool_dir.empty()) {
        throw std::runtime_error("Collector config must define spool_dir");
    }

    return config;
}

bool database_matches_filters(
    const std::string& database_path,
    const std::vector<std::string>& include_filters,
    const std::vector<std::string>& exclude_filters
)
{
    for (const auto& excluded : exclude_filters) {
        if (!excluded.empty() && database_path.find(excluded) != std::string::npos) {
            return false;
        }
    }

    if (include_filters.empty()) {
        return true;
    }

    for (const auto& included : include_filters) {
        if (!included.empty() && database_path.find(included) != std::string::npos) {
            return true;
        }
    }

    return false;
}

}  // namespace proc_usage
