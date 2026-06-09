#ifdef PROC_USAGE_ENABLE_FIREBIRD_SDK

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <mutex>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/types.h>
#include <unistd.h>
#include <utility>
#include <vector>

#include "firebird/Interface.h"
#include "proc_usage/collector.hpp"
#include "proc_usage/config.hpp"
#include "proc_usage/firebird_bridge.hpp"
#include "proc_usage/spool_writer.hpp"

namespace proc_usage::firebird {
namespace {

using Firebird::IConfig;
using Firebird::IConfigEntry;
using Firebird::IMaster;
using Firebird::IPluginBase;
using Firebird::IPluginConfig;
using Firebird::IPluginManager;
using Firebird::IReferenceCounted;
using Firebird::IStatus;
using Firebird::ITraceDatabaseConnection;
using Firebird::ITraceFactory;
using Firebird::ITraceInitInfo;
using Firebird::ITracePlugin;
using Firebird::ITraceProcedure;
using Firebird::ITraceSQLStatement;
using Firebird::ITraceTransaction;
using Firebird::PerformanceInfo;
using Firebird::ThrowStatusWrapper;

constexpr const char* kPluginName = "ProcUsageTrace";
constexpr const char* kEnvConfigPath = "PROC_USAGE_PLUGIN_CONFIG";

template <typename T>
class AutoReleasePtr {
public:
    // Небольшая RAII-обёртка для интерфейсов Firebird,
    // которые освобождаются вручную через release().
    // Она не даёт утечь объектам SDK при раннем выходе или исключении.
    explicit AutoReleasePtr(T* value = nullptr)
        : value_(value)
    {
    }

    AutoReleasePtr(const AutoReleasePtr&) = delete;
    AutoReleasePtr& operator=(const AutoReleasePtr&) = delete;

    AutoReleasePtr(AutoReleasePtr&& other) noexcept
        : value_(std::exchange(other.value_, nullptr))
    {
    }

    AutoReleasePtr& operator=(AutoReleasePtr&& other) noexcept
    {
        if (this != &other) {
            reset();
            value_ = std::exchange(other.value_, nullptr);
        }

        return *this;
    }

    ~AutoReleasePtr()
    {
        reset();
    }

    T* get() const
    {
        return value_;
    }

    T* operator->() const
    {
        return value_;
    }

    explicit operator bool() const
    {
        return value_ != nullptr;
    }

    void reset(T* value = nullptr)
    {
        if (value_ != nullptr) {
            value_->release();
        }
        value_ = value;
    }

private:
    T* value_;
};

class ReferenceCountedMixin {
public:
    void addRef()
    {
        // Объекты плагина Firebird живут через внутренний reference counting.
        ref_count_.fetch_add(1, std::memory_order_relaxed);
    }

    int release()
    {
        // Когда исчезает последняя ссылка, объект уничтожает сам себя.
        const int remaining = ref_count_.fetch_sub(1, std::memory_order_acq_rel) - 1;
        if (remaining == 0) {
            delete_self();
        }

        return remaining;
    }

protected:
    virtual ~ReferenceCountedMixin() = default;
    virtual void delete_self() = 0;

private:
    std::atomic<int> ref_count_ {1};
};

class PluginBaseState {
public:
    void setOwner(IReferenceCounted* owner)
    {
        // Пока эта фабрика или плагин ссылается на owner, не даём ему уничтожиться.
        if (owner != nullptr) {
            owner->addRef();
        }

        if (owner_ != nullptr) {
            owner_->release();
        }

        owner_ = owner;
    }

    IReferenceCounted* getOwner()
    {
        return owner_;
    }

protected:
    ~PluginBaseState()
    {
        if (owner_ != nullptr) {
            owner_->release();
            owner_ = nullptr;
        }
    }

private:
    IReferenceCounted* owner_ {nullptr};
};

std::string trim_copy(std::string_view text)
{
    // Общая утилита для разбора строковых значений из конфига.
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

std::vector<std::string> split_filter_list(std::string_view text)
{
    // В конфиге Firebird разделителем может быть и ';', и ',' — поддерживаем оба варианта.
    std::vector<std::string> filters;
    std::string current;

    for (const char ch : text) {
        if (ch == ';' || ch == ',') {
            const std::string trimmed = trim_copy(current);
            if (!trimmed.empty()) {
                filters.push_back(trimmed);
            }
            current.clear();
            continue;
        }

        current.push_back(ch);
    }

    const std::string trimmed = trim_copy(current);
    if (!trimmed.empty()) {
        filters.push_back(trimmed);
    }

    return filters;
}

bool parse_bool_value(std::string_view raw_value)
{
    // Разрешаем несколько привычных форматов, чтобы настройки плагина
    // можно было задавать в том виде, который удобен окружению.
    std::string value = trim_copy(raw_value);
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });

    if (value == "1" || value == "true" || value == "yes" || value == "on") {
        return true;
    }

    if (value == "0" || value == "false" || value == "no" || value == "off") {
        return false;
    }

    throw std::runtime_error("Invalid boolean value: " + std::string(raw_value));
}

SqlTextLoggingMode parse_sql_text_logging_mode(std::string_view raw_value)
{
    // Режимы именуются явно, чтобы в конфиге было понятно, зачем включён порог.
    std::string value = trim_copy(raw_value);
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });

    if (value == "all") {
        return SqlTextLoggingMode::all;
    }

    if (value == "threshold") {
        return SqlTextLoggingMode::threshold;
    }

    throw std::runtime_error("Invalid sql_text_logging_mode: " + std::string(raw_value));
}

std::optional<std::string> getenv_non_empty(const char* name)
{
    // В разных окружениях развёртывания настройки плагина может быть удобно
    // переопределять через переменные окружения.
    const char* value = std::getenv(name);
    if (value == nullptr || *value == '\0') {
        return std::nullopt;
    }

    return std::string(value);
}

std::string get_entry_value(IConfig* config, ThrowStatusWrapper* status, const char* key)
{
    // Читает одно именованное значение из блока конфигурации плагина Firebird.
    AutoReleasePtr<IConfigEntry> entry(config->find(status, key));
    if (!entry) {
        return {};
    }

    const char* value = entry->getValue();
    return value == nullptr ? std::string() : trim_copy(value);
}

std::uint64_t read_duration_ms(PerformanceInfo* perf)
{
    // В trace API длительность приходит через PerformanceInfo::pin_time.
    // Если структуры нет или значение некорректно, считаем длительность нулевой.
    if (perf == nullptr || perf->pin_time < 0) {
        return 0;
    }

    return static_cast<std::uint64_t>(perf->pin_time);
}

CollectorConfig default_config()
{
    // Безопасные значения по умолчанию на случай, если плагин загружен без явного конфига.
    CollectorConfig config;
    config.spool_dir = std::filesystem::temp_directory_path() / "firebird_proc_usage_spool";
    config.debug_log_path = std::filesystem::temp_directory_path() / "proc_usage_trace_debug.log";
    return config;
}

CollectorConfig load_collector_config_from_plugin(
    ThrowStatusWrapper* status,
    IPluginConfig* plugin_config
)
{
    // Наивысший приоритет у внешнего конфиг-файла, путь к которому задан
    // через переменную окружения.
    // Это удобно, если при развёртывании не хочется пересобирать настройки плагина.
    if (const auto env_path = getenv_non_empty(kEnvConfigPath)) {
        return load_collector_config_from_file(*env_path);
    }

    // Иначе начинаем со значений по умолчанию и затем переопределяем их
    // настройками из конфигурации плагина Firebird.
    CollectorConfig config = default_config();
    if (plugin_config == nullptr) {
        return config;
    }

    AutoReleasePtr<IConfig> raw_config(plugin_config->getDefaultConfig(status));
    if (!raw_config) {
        return config;
    }

    // Каждая настройка здесь необязательна: значения по умолчанию
    // заменяются только непустыми значениями.
    if (const std::string spool_dir = get_entry_value(raw_config.get(), status, "spool_dir"); !spool_dir.empty()) {
        config.spool_dir = spool_dir;
    }

    if (const std::string interval = get_entry_value(raw_config.get(), status, "flush_interval_sec"); !interval.empty()) {
        config.flush_interval = std::chrono::seconds(std::stoll(interval));
    }

    if (const std::string debug_log_path = get_entry_value(raw_config.get(), status, "debug_log_path"); !debug_log_path.empty()) {
        config.debug_log_path = debug_log_path;
    }

    if (const std::string enable_sql_stats = get_entry_value(raw_config.get(), status, "enable_sql_stats"); !enable_sql_stats.empty()) {
        config.enable_sql_stats = parse_bool_value(enable_sql_stats);
    }

    if (const std::string enable_sql_text_stats = get_entry_value(raw_config.get(), status, "enable_sql_text_stats"); !enable_sql_text_stats.empty()) {
        config.enable_sql_text_stats = parse_bool_value(enable_sql_text_stats);
    }

    if (const std::string sql_text_logging_mode = get_entry_value(raw_config.get(), status, "sql_text_logging_mode"); !sql_text_logging_mode.empty()) {
        config.sql_text_logging_mode = parse_sql_text_logging_mode(sql_text_logging_mode);
    }

    if (const std::string sql_text_min_duration_ms = get_entry_value(raw_config.get(), status, "sql_text_min_duration_ms"); !sql_text_min_duration_ms.empty()) {
        config.sql_text_min_duration_ms = static_cast<std::uint64_t>(std::stoull(sql_text_min_duration_ms));
    }

    if (const std::string include = get_entry_value(raw_config.get(), status, "include_databases"); !include.empty()) {
        config.include_databases = split_filter_list(include);
    }

    if (const std::string exclude = get_entry_value(raw_config.get(), status, "exclude_databases"); !exclude.empty()) {
        config.exclude_databases = split_filter_list(exclude);
    }

    return config;
}

std::string format_debug_timestamp(std::chrono::system_clock::time_point now)
{
    // Формат совпадает с timestamp в spool-файлах, чтобы их было легче сопоставлять с логами.
    const std::time_t raw_time = std::chrono::system_clock::to_time_t(now);
    const auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()
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

class DebugLog {
public:
    explicit DebugLog(std::filesystem::path path)
        : path_(std::move(path))
    {
    }

    void write(std::string_view message) const
    {
        // Пустой путь означает, что отладочный лог фактически отключён.
        if (path_.empty()) {
            return;
        }

        std::lock_guard<std::mutex> guard(mutex_);
        // Firebird может вызывать trace-хуки из разных рабочих потоков,
        // поэтому дозапись в лог сериализуем, чтобы он оставался читаемым.
        std::error_code error;
        const auto parent = path_.parent_path();
        if (!parent.empty()) {
            std::filesystem::create_directories(parent, error);
        }

        std::ofstream output(path_, std::ios::out | std::ios::app);
        if (!output) {
            return;
        }

        output << format_debug_timestamp(std::chrono::system_clock::now())
               << " [pid=" << ::getpid() << "] "
               << message
               << '\n';
    }

private:
    std::filesystem::path path_;
    mutable std::mutex mutex_;
};

class ProcUsageTracePlugin final
    : public Firebird::ITracePluginImpl<ProcUsageTracePlugin, ThrowStatusWrapper>,
      private ReferenceCountedMixin {
public:
    explicit ProcUsageTracePlugin(CollectorConfig config)
        : enable_sql_stats_(config.enable_sql_stats),
          enable_sql_text_stats_(config.enable_sql_text_stats),
          sql_text_logging_mode_(config.sql_text_logging_mode),
          sql_text_min_duration_ms_(config.sql_text_min_duration_ms),
          debug_log_(std::make_shared<DebugLog>(config.debug_log_path)),
          spool_dir_(config.spool_dir),
          writer_(std::make_shared<JsonlSpoolWriter>(spool_dir_)),
          collector_(std::move(config), writer_),
          bridge_(collector_)
    {
        // Для каждого экземпляра trace-плагина держим свой коллектор,
        // чтобы счётчики оставались локальными для процесса Firebird,
        // который обслуживает конкретное подключение.
        debug_log_->write("trace plugin instance created; spool_dir=" + spool_dir_.string());
    }

    ~ProcUsageTracePlugin() override
    {
        try {
            // При завершении пытаемся сделать финальный сброс,
            // чтобы не потерять последние накопленные счётчики.
            debug_log_->write("trace plugin shutdown flush requested");
            collector_.flush_now();
            debug_log_->write("trace plugin shutdown flush completed");
        }
        catch (const std::exception& exception) {
            last_error_ = exception.what();
            debug_log_->write("trace plugin shutdown flush failed: " + last_error_);
        }
        catch (...) {
            last_error_ = "Unknown exception while flushing collector during shutdown";
            debug_log_->write(last_error_);
        }
    }

    void addRef() override
    {
        ReferenceCountedMixin::addRef();
    }

    int release() override
    {
        return ReferenceCountedMixin::release();
    }

    const char* trace_get_error() override
    {
        // Firebird может запросить у плагина человекочитаемое описание последней ошибки.
        if (!last_error_.empty()) {
            debug_log_->write("trace_get_error requested: " + last_error_);
        }
        return last_error_.c_str();
    }

    FB_BOOLEAN trace_attach(ITraceDatabaseConnection*, FB_BOOLEAN, unsigned) override
    {
        debug_log_->write("trace_attach");
        return true;
    }

    FB_BOOLEAN trace_detach(ITraceDatabaseConnection*, FB_BOOLEAN) override
    {
        // Подключение закрывается, поэтому это удобный момент
        // для сброса накопленной статистики.
        debug_log_->write("trace_detach; flushing counters");
        return flush_with_error_capture();
    }

    FB_BOOLEAN trace_transaction_start(ITraceDatabaseConnection*, ITraceTransaction*, unsigned, const unsigned char*, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_transaction_end(ITraceDatabaseConnection*, ITraceTransaction*, FB_BOOLEAN, FB_BOOLEAN, unsigned) override
    {
        // Дополнительно делаем сброс на границах транзакций,
        // чтобы даже долгоживущие подключения регулярно отдавали данные.
        return flush_with_error_capture();
    }

    FB_BOOLEAN trace_proc_execute(
        ITraceDatabaseConnection* connection,
        ITraceTransaction*,
        ITraceProcedure* procedure,
        FB_BOOLEAN started,
        unsigned
    ) override
    {
        // Для timing нужен именно finish-хук, где Firebird уже знает duration.
        if (started || connection == nullptr || procedure == nullptr) {
            return true;
        }

        try {
            const char* database_name = connection->getDatabaseName();
            const char* procedure_name = procedure->getProcName();
            const std::uint64_t duration_ms = read_duration_ms(procedure->getPerf());

            bridge_.on_procedure_finish(
                database_name == nullptr ? std::string_view() : std::string_view(database_name),
                procedure_name == nullptr ? std::string_view() : std::string_view(procedure_name),
                duration_ms
            );
            debug_log_->write(
                "trace_proc_execute finish db=" +
                std::string(database_name == nullptr ? "" : database_name) +
                " proc=" +
                std::string(procedure_name == nullptr ? "" : procedure_name) +
                " duration_ms=" +
                std::to_string(duration_ms)
            );
            return true;
        }
        catch (const std::exception& exception) {
            last_error_ = exception.what();
            debug_log_->write("trace_proc_execute failed: " + last_error_);
            return false;
        }
        catch (...) {
            last_error_ = "Unknown exception in trace_proc_execute";
            debug_log_->write(last_error_);
            return false;
        }
    }

    FB_BOOLEAN trace_trigger_execute(ITraceDatabaseConnection*, ITraceTransaction*, Firebird::ITraceTrigger*, FB_BOOLEAN, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_set_context(ITraceDatabaseConnection*, ITraceTransaction*, Firebird::ITraceContextVariable*) override
    {
        return true;
    }

    FB_BOOLEAN trace_dsql_prepare(ITraceDatabaseConnection*, ITraceTransaction*, ITraceSQLStatement*, ISC_INT64, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_dsql_free(ITraceDatabaseConnection*, ITraceSQLStatement*, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_dsql_execute(
        ITraceDatabaseConnection* connection,
        ITraceTransaction*,
        ITraceSQLStatement* statement,
        FB_BOOLEAN started,
        unsigned
    ) override
    {
        // SQL-статистика включается отдельно. И, как у процедур, meaningful
        // timing доступен только на finish-событии, а не на старте.
        if ((!enable_sql_stats_ && !enable_sql_text_stats_) || started || connection == nullptr || statement == nullptr) {
            return true;
        }

        try {
            const char* database_name = connection->getDatabaseName();
            const char* sql_text = statement->getTextUTF8();
            const std::uint64_t duration_ms = read_duration_ms(statement->getPerf());

            bridge_.on_sql_finish(
                database_name == nullptr ? std::string_view() : std::string_view(database_name),
                sql_text == nullptr ? std::string_view() : std::string_view(sql_text),
                duration_ms,
                enable_sql_stats_,
                should_collect_sql_text(duration_ms)
            );
            debug_log_->write(
                "trace_dsql_execute finish db=" +
                std::string(database_name == nullptr ? "" : database_name) +
                " duration_ms=" +
                std::to_string(duration_ms)
            );
            return true;
        }
        catch (const std::exception& exception) {
            last_error_ = exception.what();
            debug_log_->write("trace_dsql_execute failed: " + last_error_);
            return false;
        }
        catch (...) {
            last_error_ = "Unknown exception in trace_dsql_execute";
            debug_log_->write(last_error_);
            return false;
        }
    }

    FB_BOOLEAN trace_blr_compile(ITraceDatabaseConnection*, ITraceTransaction*, Firebird::ITraceBLRStatement*, ISC_INT64, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_blr_execute(ITraceDatabaseConnection*, ITraceTransaction*, Firebird::ITraceBLRStatement*, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_dyn_execute(ITraceDatabaseConnection*, ITraceTransaction*, Firebird::ITraceDYNRequest*, ISC_INT64, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_service_attach(Firebird::ITraceServiceConnection*, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_service_start(Firebird::ITraceServiceConnection*, unsigned, const char*, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_service_query(Firebird::ITraceServiceConnection*, unsigned, const unsigned char*, unsigned, const unsigned char*, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_service_detach(Firebird::ITraceServiceConnection*, unsigned) override
    {
        // Для service-подключений это тоже может быть последняя возможность
        // опубликовать накопленные счётчики.
        debug_log_->write("trace_service_detach; flushing counters");
        return flush_with_error_capture();
    }

    FB_BOOLEAN trace_event_error(Firebird::ITraceConnection*, Firebird::ITraceStatusVector*, const char*) override
    {
        return true;
    }

    FB_BOOLEAN trace_event_sweep(ITraceDatabaseConnection*, Firebird::ITraceSweepInfo*, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_func_execute(ITraceDatabaseConnection*, ITraceTransaction*, Firebird::ITraceFunction*, FB_BOOLEAN, unsigned) override
    {
        return true;
    }

private:
    bool should_collect_sql_text(std::uint64_t duration_ms) const
    {
        if (!enable_sql_text_stats_) {
            return false;
        }

        if (sql_text_logging_mode_ == SqlTextLoggingMode::all) {
            return true;
        }

        return duration_ms >= sql_text_min_duration_ms_;
    }

    void delete_self() override
    {
        delete this;
    }

    FB_BOOLEAN flush_with_error_capture()
    {
        try {
            // Логируем объём накопленных данных, чтобы было проще понять,
            // почему не появились spool-файлы:
            // из-за отсутствия колбэков или из-за проблем записи на диск.
            const std::size_t pending_entries = collector_.pending_entry_count();
            const std::uint64_t pending_calls = collector_.pending_total_calls();
            debug_log_->write(
                "flush requested; pending_entries=" + std::to_string(pending_entries) +
                " pending_calls=" + std::to_string(pending_calls)
            );
            const bool wrote_file = collector_.flush_now();
            debug_log_->write(
                "flush completed; wrote_file=" + std::string(wrote_file ? "true" : "false")
            );
            return true;
        }
        catch (const std::exception& exception) {
            last_error_ = exception.what();
            debug_log_->write("flush failed: " + last_error_);
            return false;
        }
        catch (...) {
            last_error_ = "Unknown exception while flushing collector";
            debug_log_->write(last_error_);
            return false;
        }
    }

    bool enable_sql_stats_ {false};
    bool enable_sql_text_stats_ {false};
    SqlTextLoggingMode sql_text_logging_mode_ {SqlTextLoggingMode::all};
    std::uint64_t sql_text_min_duration_ms_ {0};
    // Общий отладочный лог для этого экземпляра плагина.
    std::shared_ptr<DebugLog> debug_log_;
    // Хранится в основном для логов и удобства просмотра;
    // writer тоже хранит тот же путь у себя.
    std::filesystem::path spool_dir_;
    // Реализация вывода, которая создаёт пакетные JSONL-файлы.
    std::shared_ptr<JsonlSpoolWriter> writer_;
    // Накопитель счётчиков вызовов процедур в памяти.
    UsageCollector collector_;
    // Адаптер, который превращает trace-данные Firebird в вызовы коллектора.
    FirebirdTraceBridge bridge_;
    // Текст последней ошибки, который можно вернуть обратно Firebird.
    std::string last_error_;
};

class ProcUsageTraceFactory final
    : public Firebird::ITraceFactoryImpl<ProcUsageTraceFactory, ThrowStatusWrapper>,
      private ReferenceCountedMixin,
      private PluginBaseState {
public:
    explicit ProcUsageTraceFactory(CollectorConfig config)
        : config_(std::move(config)),
          debug_log_(std::make_shared<DebugLog>(config_.debug_log_path))
    {
        debug_log_->write(
            "trace factory created; spool_dir=" + config_.spool_dir.string() +
            " flush_interval_sec=" + std::to_string(config_.flush_interval.count())
        );
    }

    void addRef() override
    {
        ReferenceCountedMixin::addRef();
    }

    int release() override
    {
        return ReferenceCountedMixin::release();
    }

    void setOwner(IReferenceCounted* owner) override
    {
        PluginBaseState::setOwner(owner);
    }

    IReferenceCounted* getOwner() override
    {
        return PluginBaseState::getOwner();
    }

    ISC_UINT64 trace_needs() override
    {
        // Просим у Firebird только события вызова процедур
        // и события жизненного цикла, которые нужны для сброса счётчиков.
        // Это уменьшает лишний trace-шум и не заставляет плагин получать
        // те события, которые он всё равно не обрабатывает.
        ISC_UINT64 flags = (ISC_UINT64{1} << ITraceFactory::TRACE_EVENT_PROC_EXECUTE) |
                           (ISC_UINT64{1} << ITraceFactory::TRACE_EVENT_TRANSACTION_END) |
                           (ISC_UINT64{1} << ITraceFactory::TRACE_EVENT_DETACH) |
                           (ISC_UINT64{1} << ITraceFactory::TRACE_EVENT_SERVICE_DETACH);
        if (config_.enable_sql_stats || config_.enable_sql_text_stats) {
            flags |= (ISC_UINT64{1} << ITraceFactory::TRACE_EVENT_DSQL_EXECUTE);
        }
        return flags;
    }

    ITracePlugin* trace_create(ThrowStatusWrapper*, ITraceInitInfo*) override
    {
        // Firebird вызывает это, когда ему нужен реальный экземпляр trace-плагина для сессии.
        debug_log_->write("trace_create invoked");
        return new ProcUsageTracePlugin(config_);
    }

private:
    void delete_self() override
    {
        delete this;
    }

    CollectorConfig config_;
    std::shared_ptr<DebugLog> debug_log_;
};

class ProcUsagePluginFactory final
    : public Firebird::IPluginFactoryImpl<ProcUsagePluginFactory, ThrowStatusWrapper> {
public:
    IPluginBase* createPlugin(ThrowStatusWrapper* status, IPluginConfig* factory_parameter) override
    {
        // Точка входа фабрики, которую Firebird вызывает во время создания плагина.
        const CollectorConfig config = load_collector_config_from_plugin(status, factory_parameter);
        DebugLog(config.debug_log_path).write(
            "createPlugin called; spool_dir=" + config.spool_dir.string()
        );
        return new ProcUsageTraceFactory(config);
    }
};

class ProcUsagePluginModule final
    : public Firebird::IPluginModuleImpl<ProcUsagePluginModule, ThrowStatusWrapper> {
public:
    void doClean() override
    {
        // Глобальных ресурсов, требующих явной очистки на уровне модуля, нет.
    }

    void threadDetach() override
    {
        // Потокоспецифичного состояния тоже нет: всё важное живёт в экземплярах.
    }
};

ProcUsagePluginFactory g_plugin_factory;
ProcUsagePluginModule g_plugin_module;

}  // namespace

}  // namespace proc_usage::firebird

extern "C" FB_DLL_EXPORT void FB_PLUGIN_ENTRY_POINT(Firebird::IMaster* master)
{
    // Регистрируем модуль и фабрику trace-плагина в менеджере плагинов Firebird.
    // После этого Firebird сможет создавать плагин по имени.
    auto* plugin_manager = master->getPluginManager();
    plugin_manager->registerModule(&proc_usage::firebird::g_plugin_module);
    plugin_manager->registerPluginFactory(
        Firebird::IPluginManager::TYPE_TRACE,
        proc_usage::firebird::kPluginName,
        &proc_usage::firebird::g_plugin_factory
    );
}

#endif
