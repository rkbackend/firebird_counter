#pragma once

#include <filesystem>
#include <string>
#include <vector>

#include "proc_usage/collector.hpp"

namespace proc_usage {

// Конкретная реализация записи, которая сохраняет накопленные данные
// в файлы формата JSON Lines.
// Одна строка = один JSON-объект, это упрощает потоковую обработку дальше.
class JsonlSpoolWriter final : public SpoolWriter {
public:
    explicit JsonlSpoolWriter(std::filesystem::path spool_dir);

    bool write_records(const std::vector<FlushRecord>& records) override;

private:
    // Экранирует символы, которые иначе сломали бы синтаксис JSON.
    static std::string escape_json_string(const std::string& input);
    // Сначала данные пишутся во временный файл, а после успешной записи
    // он переименовывается.
    std::filesystem::path build_temp_path() const;
    // Итоговое имя файла становится видно потребителям только после rename().
    std::filesystem::path build_final_path() const;

    std::filesystem::path spool_dir_;
};

}  // namespace proc_usage
