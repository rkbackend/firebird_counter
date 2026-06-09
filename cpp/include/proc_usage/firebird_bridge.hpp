#pragma once

#include <chrono>
#include <cstdint>
#include <string>
#include <string_view>

#include "proc_usage/collector.hpp"

namespace proc_usage::firebird {

class FirebirdTraceBridge {
public:
    explicit FirebirdTraceBridge(UsageCollector& collector);

    void on_procedure_finish(
        std::string_view database_path,
        std::string_view procedure_name,
        std::uint64_t duration_ms,
        std::chrono::system_clock::time_point now = std::chrono::system_clock::now()
    );

    void on_sql_finish(
        std::string_view database_path,
        std::string_view sql_text,
        std::uint64_t duration_ms,
        std::chrono::system_clock::time_point now = std::chrono::system_clock::now()
    );

private:
    static std::string trim_identifier(std::string_view text);
    static std::string classify_sql_statement(std::string_view sql_text);

    UsageCollector& collector_;
};

}  // namespace proc_usage::firebird
