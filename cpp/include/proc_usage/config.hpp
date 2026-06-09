#pragma once

#include <chrono>
#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace proc_usage {

enum class SqlTextLoggingMode {
    // Сохранять полный SQL-текст для каждого завершённого SQL statement.
    all,
    // Сохранять полный SQL-текст только если длительность не меньше порога.
    threshold,
};

// Настройки времени выполнения для коллектора и плагина.
// Структура специально сделана простой, потому что она загружается либо
// из небольшого текстового конфига, либо из настроек плагина Firebird.
struct CollectorConfig {
    // Как часто разрешено сбрасывать накопленные счётчики.
    std::chrono::seconds flush_interval {30};
    // Каталог, куда записываются пакетные JSONL-файлы.
    std::filesystem::path spool_dir;
    // Необязательный диагностический лог, который использует плагин Firebird.
    std::filesystem::path debug_log_path;
    // Если true, дополнительно собирать timing обычных SQL statement'ов.
    bool enable_sql_stats {false};
    // Если true, дополнительно собирать timing по полному тексту SQL-запроса.
    bool enable_sql_text_stats {false};
    // Управляет тем, писать ли все SQL-тексты или только "долгие".
    SqlTextLoggingMode sql_text_logging_mode {SqlTextLoggingMode::all};
    // Порог в миллисекундах для режима `threshold`.
    std::uint64_t sql_text_min_duration_ms {0};
    // Если список не пуст, будут учитываться только базы, путь которых содержит
    // один из этих фрагментов.
    std::vector<std::string> include_databases;
    // Базы, путь которых содержит один из этих фрагментов, всегда игнорируются.
    std::vector<std::string> exclude_databases;
};

// Загружает простой конфиг коллектора в формате "key = value".
CollectorConfig load_collector_config_from_file(const std::filesystem::path& path);

// Возвращает true, если путь к базе нужно учитывать согласно include/exclude фильтрам.
bool database_matches_filters(
    const std::string& database_path,
    const std::vector<std::string>& include_filters,
    const std::vector<std::string>& exclude_filters
);

}  // namespace proc_usage
