#include "proc_usage/firebird_bridge.hpp"

#include <algorithm>
#include <cctype>

namespace proc_usage::firebird {
namespace {

std::string to_upper_copy(std::string_view text)
{
    // SQL без учёта регистра проще разбирать, если сначала привести его к верхнему регистру.
    std::string result(text);
    std::transform(result.begin(), result.end(), result.begin(), [](unsigned char ch) {
        return static_cast<char>(std::toupper(ch));
    });
    return result;
}

}  // namespace

FirebirdTraceBridge::FirebirdTraceBridge(UsageCollector& collector)
    : collector_(collector)
{
}

void FirebirdTraceBridge::on_procedure_execute(
    std::string_view database_path,
    std::string_view procedure_name,
    std::chrono::system_clock::time_point now
)
{
    // Firebird может передать идентификатор с внешними кавычками или пробелами,
    // поэтому сначала нормализуем имя, а потом используем его как часть ключа.
    const std::string normalized_name = trim_identifier(procedure_name);
    if (normalized_name.empty()) {
        return;
    }

    // Превращаем string_view во владение std::string, потому что коллектор хранит данные у себя.
    collector_.record_call(std::string(database_path), normalized_name, now);
}

std::optional<std::string> FirebirdTraceBridge::extract_procedure_name_from_sql(std::string_view sql_text) const
{
    // Быстрый разбор самого типичного вида запроса:
    // EXECUTE PROCEDURE MY_PROC(...)
    const std::string upper_sql = to_upper_copy(sql_text);
    const std::string prefix = "EXECUTE PROCEDURE ";

    if (!upper_sql.starts_with(prefix)) {
        return std::nullopt;
    }

    const std::size_t start = prefix.size();
    std::size_t end = start;

    // Имя процедуры заканчивается на первом пробеле или открывающей скобке.
    while (end < sql_text.size()) {
        const char ch = sql_text[end];
        if (std::isspace(static_cast<unsigned char>(ch)) || ch == '(') {
            break;
        }
        ++end;
    }

    const std::string name = trim_identifier(sql_text.substr(start, end - start));
    if (name.empty()) {
        return std::nullopt;
    }

    return name;
}

std::string FirebirdTraceBridge::trim_identifier(std::string_view text)
{
    std::size_t start = 0;
    std::size_t end = text.size();

    // Сначала убираем пробелы по краям.
    while (start < end && std::isspace(static_cast<unsigned char>(text[start]))) {
        ++start;
    }

    while (end > start && std::isspace(static_cast<unsigned char>(text[end - 1]))) {
        --end;
    }

    // В Firebird SQL идентификаторы могут быть заключены в двойные кавычки.
    // Если кавычки обрамляют весь токен, снимаем их.
    if (end > start && text[start] == '"' && text[end - 1] == '"') {
        ++start;
        --end;
    }

    return std::string(text.substr(start, end - start));
}

}  // namespace proc_usage::firebird
