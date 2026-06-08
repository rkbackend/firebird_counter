#pragma once

#include <chrono>
#include <optional>
#include <string>
#include <string_view>

#include "proc_usage/collector.hpp"

namespace proc_usage::firebird {

// Небольшой адаптер между "сырыми" trace-колбэками Firebird
// и универсальным коллектором.
// Его задача — нормализация данных, а не их хранение.
class FirebirdTraceBridge {
public:
    explicit FirebirdTraceBridge(UsageCollector& collector);

    // Это узкая точка интеграции с trace-колбэками Firebird.
    // Когда плагин получает событие вызова процедуры, он передаёт сюда
    // уже нормализованные имена.
    void on_procedure_execute(
        std::string_view database_path,
        std::string_view procedure_name,
        std::chrono::system_clock::time_point now = std::chrono::system_clock::now()
    );

    // В некоторых trace-сценариях доступен только SQL-текст.
    // Этот метод извлекает имя процедуры из самого типичного шаблона
    // "EXECUTE PROCEDURE ..." как безопасный запасной вариант.
    std::optional<std::string> extract_procedure_name_from_sql(std::string_view sql_text) const;

private:
    // Убирает пробелы по краям и снимает внешние двойные кавычки
    // с SQL-идентификатора.
    static std::string trim_identifier(std::string_view text);

    UsageCollector& collector_;
};

}  // namespace proc_usage::firebird
