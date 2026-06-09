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
    // Удаляем пробелы в начале и в конце строки прямо в этом объекте.
    auto not_space = [](unsigned char ch) { return !std::isspace(ch); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), not_space));
    value.erase(std::find_if(value.rbegin(), value.rend(), not_space).base(), value.end());
    return value;
}

std::vector<std::string> split_list(const std::string& value)
{
    // В конфиге элементы списка разделяются через ';':
    // include_databases = db1;db2;db3
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

bool parse_bool_value(const std::string& raw_value)
{
    // Поддерживаем несколько привычных форматов булевых значений, чтобы
    // конфиг было удобнее писать и переносить между окружениями.
    std::string value = raw_value;
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });

    if (value == "1" || value == "true" || value == "yes" || value == "on") {
        return true;
    }

    if (value == "0" || value == "false" || value == "no" || value == "off") {
        return false;
    }

    throw std::runtime_error("Invalid boolean value: " + raw_value);
}

}  // namespace

CollectorConfig load_collector_config_from_file(const std::filesystem::path& path)
{
    // Формат deliberately простой: обычный текстовый файл с `key = value`.
    // Его удобно править прямо на сервере без специальных утилит.
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

        // Пропускаем пустые строки и комментарии, чтобы конфиг оставался удобным для человека.
        if (line.empty() || line.starts_with('#')) {
            continue;
        }

        // Каждая непустая строка без комментария должна иметь вид:
        // key = value
        const auto separator = line.find('=');
        if (separator == std::string::npos) {
            throw std::runtime_error("Invalid config line " + std::to_string(line_number) + ": " + line);
        }

        const std::string key = trim(line.substr(0, separator));
        const std::string value = trim(line.substr(separator + 1));

        // Каждый ключ разбирается явно. Это строже, зато опечатки в конфиге
        // всплывают сразу, а не приводят к молчаливому запуску с дефолтами.
        if (key == "spool_dir") {
            config.spool_dir = value;
        }
        else if (key == "debug_log_path") {
            config.debug_log_path = value;
        }
        else if (key == "flush_interval_sec") {
            config.flush_interval = std::chrono::seconds(std::stoll(value));
        }
        else if (key == "enable_sql_stats") {
            config.enable_sql_stats = parse_bool_value(value);
        }
        else if (key == "enable_sql_text_stats") {
            config.enable_sql_text_stats = parse_bool_value(value);
        }
        else if (key == "include_databases") {
            config.include_databases = split_list(value);
        }
        else if (key == "exclude_databases") {
            config.exclude_databases = split_list(value);
        }
        else {
            // Неизвестные ключи считаем ошибкой, чтобы сразу ловить опечатки.
            throw std::runtime_error("Unknown config key: " + key);
        }
    }

    // Без spool_dir плагин физически не знает, куда писать JSONL-пакеты.
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
    // Правила exclude имеют приоритет и срабатывают сразу.
    for (const auto& excluded : exclude_filters) {
        if (!excluded.empty() && database_path.find(excluded) != std::string::npos) {
            return false;
        }
    }

    // Если include-правил нет, значит учитываем всё, что не было исключено.
    if (include_filters.empty()) {
        return true;
    }

    // Если include-правила есть, должен совпасть хотя бы один шаблон.
    for (const auto& included : include_filters) {
        if (!included.empty() && database_path.find(included) != std::string::npos) {
            return true;
        }
    }

    return false;
}

}  // namespace proc_usage
