#include "proc_usage/spool_writer.hpp"

#include <chrono>
#include <fstream>
#include <iomanip>
#include <random>
#include <sstream>
#include <stdexcept>

namespace proc_usage {
namespace {

std::string usage_kind_to_string(UsageKind kind)
{
    switch (kind) {
    case UsageKind::procedure:
        return "procedure";
    case UsageKind::sql:
        return "sql";
    case UsageKind::sql_text:
        return "sql_text";
    }

    return "unknown";
}

std::string format_timestamp(std::chrono::system_clock::time_point timestamp)
{
    // Преобразуем C++ time_point в строку UTC, например:
    // 2026-06-08T10:11:12.345Z
    const std::time_t raw_time = std::chrono::system_clock::to_time_t(timestamp);
    const auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(
        timestamp.time_since_epoch()
    ) % 1000;

    std::tm utc_time {};
#if defined(_WIN32)
    gmtime_s(&utc_time, &raw_time);
#else
    gmtime_r(&raw_time, &utc_time);
#endif

    std::ostringstream output;
    output << std::put_time(&utc_time, "%Y-%m-%dT%H:%M:%S")
           << '.'
           << std::setw(3)
           << std::setfill('0')
           << milliseconds.count()
           << 'Z';
    return output.str();
}

}  // namespace

JsonlSpoolWriter::JsonlSpoolWriter(std::filesystem::path spool_dir)
    : spool_dir_(std::move(spool_dir))
{
}

bool JsonlSpoolWriter::write_records(const std::vector<FlushRecord>& records)
{
    if (records.empty()) {
        return false;
    }

    // Убеждаемся, что целевой каталог существует, прежде чем создавать в нём файлы.
    std::filesystem::create_directories(spool_dir_);

    // Сначала пишем во временный файл, чтобы читатели никогда не увидели
    // недописанный пакет.
    const auto temp_path = build_temp_path();
    const auto final_path = build_final_path();

    {
        std::ofstream output(temp_path, std::ios::out | std::ios::trunc);
        if (!output) {
            throw std::runtime_error("Unable to open spool temp file: " + temp_path.string());
        }

        for (const auto& record : records) {
            // Каждая строка — это отдельный JSON-объект с уже агрегированными метриками.
            output << "{\"ts\":\"" << format_timestamp(record.timestamp)
                   << "\",\"kind\":\"" << usage_kind_to_string(record.kind)
                   << "\",\"hour\":\"" << escape_json_string(record.usage_hour)
                   << "\",\"db\":\"" << escape_json_string(record.database)
                   << "\",\"name\":\"" << escape_json_string(record.name)
                   << "\",\"count\":" << record.count
                   << ",\"total_time_ms\":" << record.total_time_ms
                   << ",\"min_time_ms\":" << record.min_time_ms
                   << ",\"max_time_ms\":" << record.max_time_ms
                   << "}\n";
        }
    }

    // Атомарный rename — это момент "публикации": потребители должны видеть
    // только уже готовый итоговый файл.
    std::filesystem::rename(temp_path, final_path);
    return true;
}

std::string JsonlSpoolWriter::escape_json_string(const std::string& input)
{
    std::ostringstream output;

    for (const char ch : input) {
        // Экранируем только те символы, которые могут сломать структуру
        // или смысл JSON.
        switch (ch) {
        case '\\':
            output << "\\\\";
            break;
        case '"':
            output << "\\\"";
            break;
        case '\n':
            output << "\\n";
            break;
        case '\r':
            output << "\\r";
            break;
        case '\t':
            output << "\\t";
            break;
        default:
            output << ch;
            break;
        }
    }

    return output.str();
}

std::filesystem::path JsonlSpoolWriter::build_temp_path() const
{
    // Комбинируем текущее время со случайной добавкой, чтобы снизить риск
    // совпадения имён, если несколько процессов пишут в один каталог.
    const auto now = std::chrono::system_clock::now().time_since_epoch().count();
    std::random_device random_device;
    const auto noise = random_device();
    return spool_dir_ / ("proc_usage_" + std::to_string(now) + "_" + std::to_string(noise) + ".jsonl.tmp");
}

std::filesystem::path JsonlSpoolWriter::build_final_path() const
{
    // Для итогового файла используется та же стратегия уникальности,
    // только без суффикса ".tmp".
    const auto now = std::chrono::system_clock::now().time_since_epoch().count();
    std::random_device random_device;
    const auto noise = random_device();
    return spool_dir_ / ("proc_usage_" + std::to_string(now) + "_" + std::to_string(noise) + ".jsonl");
}

}  // namespace proc_usage
