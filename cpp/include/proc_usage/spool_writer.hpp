#pragma once

#include <filesystem>
#include <string>
#include <vector>

#include "proc_usage/collector.hpp"

namespace proc_usage {

class JsonlSpoolWriter final : public SpoolWriter {
public:
    explicit JsonlSpoolWriter(std::filesystem::path spool_dir);

    bool write_records(const std::vector<FlushRecord>& records) override;

private:
    static std::string escape_json_string(const std::string& input);
    std::filesystem::path build_temp_path() const;
    std::filesystem::path build_final_path() const;

    std::filesystem::path spool_dir_;
};

}  // namespace proc_usage

