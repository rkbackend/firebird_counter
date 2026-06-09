#include "proc_usage/firebird_bridge.hpp"

#include <algorithm>
#include <cctype>

namespace proc_usage::firebird {
namespace {

std::string to_upper_copy(std::string_view text)
{
    std::string result(text);
    std::transform(result.begin(), result.end(), result.begin(), [](unsigned char ch) {
        return static_cast<char>(std::toupper(ch));
    });
    return result;
}

std::string trim_copy(std::string_view text)
{
    std::size_t start = 0;
    std::size_t end = text.size();

    while (start < end && std::isspace(static_cast<unsigned char>(text[start]))) {
        ++start;
    }

    while (end > start && std::isspace(static_cast<unsigned char>(text[end - 1]))) {
        --end;
    }

    return std::string(text.substr(start, end - start));
}

}  // namespace

FirebirdTraceBridge::FirebirdTraceBridge(UsageCollector& collector)
    : collector_(collector)
{
}

void FirebirdTraceBridge::on_procedure_finish(
    std::string_view database_path,
    std::string_view procedure_name,
    std::uint64_t duration_ms,
    std::chrono::system_clock::time_point now
)
{
    const std::string normalized_name = trim_identifier(procedure_name);
    if (normalized_name.empty()) {
        return;
    }

    collector_.record_usage(
        UsageKind::procedure,
        std::string(database_path),
        normalized_name,
        duration_ms,
        now
    );
}

void FirebirdTraceBridge::on_sql_finish(
    std::string_view database_path,
    std::string_view sql_text,
    std::uint64_t duration_ms,
    bool collect_sql_kind,
    bool collect_sql_text,
    std::chrono::system_clock::time_point now
)
{
    const std::string database(database_path);

    if (collect_sql_kind) {
        const std::string sql_kind = classify_sql_statement(sql_text);
        collector_.record_usage(
            UsageKind::sql,
            database,
            sql_kind,
            duration_ms,
            now
        );
    }

    if (collect_sql_text && !sql_text.empty()) {
        collector_.record_usage(
            UsageKind::sql_text,
            database,
            std::string(sql_text),
            duration_ms,
            now
        );
    }
}

std::string FirebirdTraceBridge::trim_identifier(std::string_view text)
{
    std::size_t start = 0;
    std::size_t end = text.size();

    while (start < end && std::isspace(static_cast<unsigned char>(text[start]))) {
        ++start;
    }

    while (end > start && std::isspace(static_cast<unsigned char>(text[end - 1]))) {
        --end;
    }

    if (end > start && text[start] == '"' && text[end - 1] == '"') {
        ++start;
        --end;
    }

    return std::string(text.substr(start, end - start));
}

std::string FirebirdTraceBridge::classify_sql_statement(std::string_view sql_text)
{
    const std::string trimmed = trim_copy(sql_text);
    if (trimmed.empty()) {
        return "UNKNOWN";
    }

    const std::string upper_sql = to_upper_copy(trimmed);
    if (upper_sql.starts_with("EXECUTE PROCEDURE")) {
        return "EXECUTE PROCEDURE";
    }

    if (upper_sql.starts_with("EXECUTE BLOCK")) {
        return "EXECUTE BLOCK";
    }

    std::size_t end = 0;
    while (end < upper_sql.size() && !std::isspace(static_cast<unsigned char>(upper_sql[end]))) {
        ++end;
    }

    const std::string first_token = upper_sql.substr(0, end);
    return first_token.empty() ? "UNKNOWN" : first_token;
}

}  // namespace proc_usage::firebird
