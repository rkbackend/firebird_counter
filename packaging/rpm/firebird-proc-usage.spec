%bcond_without firebird_plugin

%global app_name firebird-proc-usage
%global app_conf_dir %{_sysconfdir}/firebird-proc-usage
%global firebird_conf_dir %{app_conf_dir}/firebird
%global firebird_plugins_dir %{_libdir}/firebird/plugins
%global plugin_library libproc_usage_trace.so

Name:           %{app_name}
Version:        0.1.0
Release:        1%{?dist}
Summary:        Low-overhead Firebird procedure usage collector
License:        UNLICENSED
URL:            https://example.invalid/%{app_name}
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-build
BuildRequires:  python3-devel
BuildRequires:  python3-installer
BuildRequires:  python3-setuptools
BuildRequires:  systemd-rpm-macros

%if %{with firebird_plugin}
BuildRequires:  cmake
BuildRequires:  firebird-devel
BuildRequires:  gcc-c++
BuildRequires:  make
%endif

Requires:       python3 >= 3.9

%description
Hybrid collector for Firebird stored procedure usage.
The main package installs the Python ingestion service, CLI, sample configuration,
and a systemd unit. The optional firebird-plugin subpackage installs the compiled
Firebird trace plugin shared library.

%if %{with firebird_plugin}
%package firebird-plugin
Summary:        Firebird trace plugin for %{name}
Requires:       %{name} = %{version}-%{release}
Requires:       firebird

%description firebird-plugin
Shared library for the Firebird trace plugin that writes compact JSONL snapshots
for the %{name} Python collector.
%endif

%prep
%autosetup -n %{name}-%{version}

%build
%pyproject_wheel

%if %{with firebird_plugin}
cmake -S . -B build-rpm \
    -DPROC_USAGE_ENABLE_FIREBIRD_SDK=ON \
    -DFIREBIRD_INCLUDE_DIR=%{_includedir}/firebird
cmake --build build-rpm
%endif

%install
%pyproject_install
%pyproject_save_files proc_usage

install -d %{buildroot}%{app_conf_dir}
install -d %{buildroot}%{firebird_conf_dir}
install -d %{buildroot}%{_sharedstatedir}/firebird-proc-usage
install -d %{buildroot}%{_sharedstatedir}/firebird-proc-usage/spool

install -Dpm 0644 packaging/rpm/python_service.json \
    %{buildroot}%{app_conf_dir}/python_service.json
install -Dpm 0644 packaging/rpm/proc_usage_plugin.conf \
    %{buildroot}%{firebird_conf_dir}/proc_usage_plugin.conf
install -Dpm 0644 configs/plugins.conf.sample \
    %{buildroot}%{firebird_conf_dir}/plugins.conf.sample
install -Dpm 0644 configs/firebird.conf.sample \
    %{buildroot}%{firebird_conf_dir}/firebird.conf.sample
install -Dpm 0644 configs/fbtrace.conf.sample \
    %{buildroot}%{firebird_conf_dir}/fbtrace.conf.sample
install -Dpm 0644 packaging/rpm/proc-usage.service \
    %{buildroot}%{_unitdir}/proc-usage.service

%if %{with firebird_plugin}
install -d %{buildroot}%{firebird_plugins_dir}
install -Dpm 0755 build-rpm/%{plugin_library} \
    %{buildroot}%{firebird_plugins_dir}/%{plugin_library}
%endif

%check
python3 -m unittest -q tests.test_storage tests.test_service tests.test_benchmark

%post
%systemd_post proc-usage.service
if getent passwd firebird >/dev/null 2>&1; then
    chown -R firebird:firebird %{_sharedstatedir}/firebird-proc-usage || :
fi

%preun
%systemd_preun proc-usage.service

%postun
%systemd_postun_with_restart proc-usage.service

%files -f %{pyproject_files}
%doc README.md
%dir %{app_conf_dir}
%dir %{firebird_conf_dir}
%dir %{_sharedstatedir}/firebird-proc-usage
%dir %{_sharedstatedir}/firebird-proc-usage/spool
%config(noreplace) %{app_conf_dir}/python_service.json
%config(noreplace) %{firebird_conf_dir}/proc_usage_plugin.conf
%config(noreplace) %{firebird_conf_dir}/plugins.conf.sample
%config(noreplace) %{firebird_conf_dir}/firebird.conf.sample
%config(noreplace) %{firebird_conf_dir}/fbtrace.conf.sample
%{_unitdir}/proc-usage.service

%if %{with firebird_plugin}
%files firebird-plugin
%{firebird_plugins_dir}/%{plugin_library}
%endif

%changelog
* Sun Jun 08 2026 Codex <codex@example.invalid> - 0.1.0-1
- Added RPM packaging for the Python service and optional Firebird plugin
