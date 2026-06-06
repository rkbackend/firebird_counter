#pragma once

#include <chrono>
#include <optional>
#include <string>
#include <string_view>

#include "proc_usage/collector.hpp"

namespace proc_usage::firebird {

class FirebirdTraceBridge {
public:
    explicit FirebirdTraceBridge(UsageCollector& collector);

    // This is the narrow integration seam for Firebird trace callbacks.
    // Once the plugin receives a procedure event, it forwards the normalized names here.
    void on_procedure_execute(
        std::string_view database_path,
        std::string_view procedure_name,
        std::chrono::system_clock::time_point now = std::chrono::system_clock::now()
    );

    // Some trace paths may provide only SQL text. This helper extracts the procedure
    // name from the most common "EXECUTE PROCEDURE ..." shape as a safe fallback.
    std::optional<std::string> extract_procedure_name_from_sql(std::string_view sql_text) const;

private:
    static std::string trim_identifier(std::string_view text);

    UsageCollector& collector_;
};

}  // namespace proc_usage::firebird

