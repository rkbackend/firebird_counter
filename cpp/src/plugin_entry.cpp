#ifdef PROC_USAGE_ENABLE_FIREBIRD_SDK

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
using Firebird::ITraceTransaction;
using Firebird::ThrowStatusWrapper;

constexpr const char* kPluginName = "ProcUsageTrace";
constexpr const char* kEnvConfigPath = "PROC_USAGE_PLUGIN_CONFIG";

template <typename T>
class AutoReleasePtr {
public:
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
        ref_count_.fetch_add(1, std::memory_order_relaxed);
    }

    int release()
    {
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

std::optional<std::string> getenv_non_empty(const char* name)
{
    const char* value = std::getenv(name);
    if (value == nullptr || *value == '\0') {
        return std::nullopt;
    }

    return std::string(value);
}

std::string get_entry_value(IConfig* config, ThrowStatusWrapper* status, const char* key)
{
    AutoReleasePtr<IConfigEntry> entry(config->find(status, key));
    if (!entry) {
        return {};
    }

    const char* value = entry->getValue();
    return value == nullptr ? std::string() : trim_copy(value);
}

CollectorConfig default_config()
{
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
    if (const auto env_path = getenv_non_empty(kEnvConfigPath)) {
        return load_collector_config_from_file(*env_path);
    }

    CollectorConfig config = default_config();
    if (plugin_config == nullptr) {
        return config;
    }

    AutoReleasePtr<IConfig> raw_config(plugin_config->getDefaultConfig(status));
    if (!raw_config) {
        return config;
    }

    if (const std::string spool_dir = get_entry_value(raw_config.get(), status, "spool_dir"); !spool_dir.empty()) {
        config.spool_dir = spool_dir;
    }

    if (const std::string interval = get_entry_value(raw_config.get(), status, "flush_interval_sec"); !interval.empty()) {
        config.flush_interval = std::chrono::seconds(std::stoll(interval));
    }

    if (const std::string debug_log_path = get_entry_value(raw_config.get(), status, "debug_log_path"); !debug_log_path.empty()) {
        config.debug_log_path = debug_log_path;
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
        if (path_.empty()) {
            return;
        }

        std::lock_guard<std::mutex> guard(mutex_);
        // Firebird may call trace hooks from different worker threads, so we serialize
        // appends to keep the diagnostic log readable.
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
        : debug_log_(std::make_shared<DebugLog>(config.debug_log_path)),
          spool_dir_(config.spool_dir),
          writer_(std::make_shared<JsonlSpoolWriter>(spool_dir_)),
          collector_(std::move(config), writer_),
          bridge_(collector_)
    {
        // We keep one collector per trace plugin instance so counters stay local to the
        // Firebird process handling the traced attachment.
        debug_log_->write("trace plugin instance created; spool_dir=" + spool_dir_.string());
    }

    ~ProcUsageTracePlugin() override
    {
        try {
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
        debug_log_->write("trace_detach; flushing counters");
        return flush_with_error_capture();
    }

    FB_BOOLEAN trace_transaction_start(ITraceDatabaseConnection*, ITraceTransaction*, unsigned, const unsigned char*, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_transaction_end(ITraceDatabaseConnection*, ITraceTransaction*, FB_BOOLEAN, FB_BOOLEAN, unsigned) override
    {
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
        if (!started || connection == nullptr || procedure == nullptr) {
            return true;
        }

        try {
            const char* database_name = connection->getDatabaseName();
            const char* procedure_name = procedure->getProcName();

            // The bridge owns the "count one procedure call" policy so the Firebird hook
            // stays thin and easy to debug.
            bridge_.on_procedure_execute(
                database_name == nullptr ? std::string_view() : std::string_view(database_name),
                procedure_name == nullptr ? std::string_view() : std::string_view(procedure_name)
            );
            debug_log_->write(
                "trace_proc_execute started db=" +
                std::string(database_name == nullptr ? "" : database_name) +
                " proc=" +
                std::string(procedure_name == nullptr ? "" : procedure_name)
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

    FB_BOOLEAN trace_dsql_prepare(ITraceDatabaseConnection*, ITraceTransaction*, Firebird::ITraceSQLStatement*, ISC_INT64, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_dsql_free(ITraceDatabaseConnection*, Firebird::ITraceSQLStatement*, unsigned) override
    {
        return true;
    }

    FB_BOOLEAN trace_dsql_execute(ITraceDatabaseConnection*, ITraceTransaction*, Firebird::ITraceSQLStatement*, FB_BOOLEAN, unsigned) override
    {
        return true;
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
    void delete_self() override
    {
        delete this;
    }

    FB_BOOLEAN flush_with_error_capture()
    {
        try {
            // Logging the pending counter sizes makes it obvious whether missing spool
            // files come from absent callbacks or from filesystem issues during flush.
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

    std::shared_ptr<DebugLog> debug_log_;
    std::filesystem::path spool_dir_;
    std::shared_ptr<JsonlSpoolWriter> writer_;
    UsageCollector collector_;
    FirebirdTraceBridge bridge_;
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
        // We ask only for procedure execution plus lifecycle events used to flush buffered counters.
        return (ISC_UINT64{1} << ITraceFactory::TRACE_EVENT_PROC_EXECUTE) |
               (ISC_UINT64{1} << ITraceFactory::TRACE_EVENT_TRANSACTION_END) |
               (ISC_UINT64{1} << ITraceFactory::TRACE_EVENT_DETACH) |
               (ISC_UINT64{1} << ITraceFactory::TRACE_EVENT_SERVICE_DETACH);
    }

    ITracePlugin* trace_create(ThrowStatusWrapper*, ITraceInitInfo*) override
    {
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
    }

    void threadDetach() override
    {
    }
};

ProcUsagePluginFactory g_plugin_factory;
ProcUsagePluginModule g_plugin_module;

}  // namespace

}  // namespace proc_usage::firebird

extern "C" FB_DLL_EXPORT void FB_PLUGIN_ENTRY_POINT(Firebird::IMaster* master)
{
    auto* plugin_manager = master->getPluginManager();
    plugin_manager->registerModule(&proc_usage::firebird::g_plugin_module);
    plugin_manager->registerPluginFactory(
        Firebird::IPluginManager::TYPE_TRACE,
        proc_usage::firebird::kPluginName,
        &proc_usage::firebird::g_plugin_factory
    );
}

#endif
