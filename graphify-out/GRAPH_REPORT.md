# Graph Report - operator  (2026-10-18)

## Corpus Check
- 135 files · ~111,291 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 22 file(s) not represented in the graph (top: .tgz 8, (none) 5, .tpl 5)

## Summary
- 2431 nodes · 6469 edges · 102 communities (80 shown, 22 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 706 edges (avg confidence: 0.92)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from the published tree.
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- virtfusion_operator/servers.py
- ProxmoxError
- test_virtfusion_lifecycle.py
- typing
- handlers.py
- virtfusion_operator/handlers/vm.py
- virtfusion_operator/guest_userdata.py
- solusvm_operator/handlers/vm.py
- test_solusvm_lifecycle.py
- Patch
- server.py
- proxmox_operator/config.py
- solusvm_operator/servers.py
- SolusVMClient
- VirtFusionClient
- firewall_reconcile.py
- bridge.py
- virtfusion_operator/smtp_outbound.py
- PanelClient
- pterodactyl_operator/servers.py
- catalog.py
- test_pterodactyl_client.py
- node_handler.py
- proxmox_operator/types.py
- backup_reconcile.py
- action.py
- VirtFusionHTTPError
- CustomResourceDefinition virtfusionvms.vm.virtfusion.com
- logger
- runner.py
- pytest
- test_egg_change.py
- operations.py
- run_hook_for_operation
- _vm_config_with_ips
- _client
- _config_post_kwargs
- proxmox_operator/client.py
- solusvm_operator/client.py
- unittest_mock
- CreateOperationHandler
- default_project_id
- ProxmoxClient
- files.py
- solusvm_operator/sshkeys.py
- TestGenerateCidataIso
- test_noderings_hooks.py
- Any
- reconcile_vm_smtp_outbound_block
- _FakeSVM
- load_hook_settings
- TemplateCompiler
- test_virtfusion_ipv4.py
- pterodactyl_operator/client.py
- Hook job namespace
- generate_cidata_iso
- _reconcile_firewall_rules_timer
- compute_resize_delta
- choose_egg
- virtfusion_operator/handlers/node.py
- test_pterodactyl_bridge_grants.py
- Proxmox VM Operator
- Deployment proxmox-operator
- ConfigMap proxmox-operator-alloy-config
- cloudinit_iso_filename
- _check_response
- dispatch_hook
- NetworkConfig
- Deployment pterodactyl-operator
- CPUConfig
- _get_k8s_client
- VirtFusion Operator Helm Chart
- Logger
- Pterodactyl Application API
- on_vnc_request
- PanelError
- resolve_vmid_str_from_vm_cr
- test_provisioning.py
- java_minecraft_server
- proxmox-operator Chart 0.1.9
- normalize_prefix
- projects.py
- virtfusion_operator/users.py
- first_block_disk_key_from_config
- parse_disk_size_gib
- test_action_once.py
- should_skip_operation
- Deployment virtfusion-operator-vnc-gateway
- test_restore_backup_sends_truncate
- test_virtfusion_isolation.py
- Deployment solusvm-operator-exporter
- Deployment solusvm-operator-vnc-gateway
- gameserver_hook_env
- _create_lock
- publish-crd-charts.sh
- publish-operator-charts.sh
- proxmox-operator

## God Nodes (most connected - your core abstractions)
1. `Logger` - 112 edges
2. `ProxmoxClient` - 100 edges
3. `PanelClient` - 95 edges
4. `VirtFusionClient` - 91 edges
5. `SolusVMClient` - 85 edges
6. `Patch` - 76 edges
7. `ProxmoxError` - 65 edges
8. `_run()` - 54 edges
9. `PanelError` - 42 edges
10. `SolusVMHTTPError` - 42 edges

## Surprising Connections (you probably didn't know these)
- `test_reconcile_permanent_proxmox_error_does_not_retry()` --uses--> `ProxmoxError`  [INFERRED]
  tests/test_vnc_reconcile.py → src/proxmox_operator/client.py
- `ProxmoxVM demo-vm8` --semantically_similar_to--> `CustomResourceDefinition virtfusionvms.vm.virtfusion.com`  [INFERRED] [semantically similar]
  samples/proxmoxvm.yaml → charts/virtfusion-operator-crds/templates/crds/virtfusionvm.yaml
- `TestRunHook` --uses--> `HookConcurrencyError`  [INFERRED]
  tests/test_noderings_hooks.py → src/noderings_hooks/types.py
- `TestConfigureVmCloudinitIso` --uses--> `ProxmoxError`  [INFERRED]
  tests/test_client_cloudinit_iso_cleanup.py → src/proxmox_operator/client.py
- `TestDeleteStorageContent` --uses--> `ProxmoxError`  [INFERRED]
  tests/test_client_cloudinit_iso_cleanup.py → src/proxmox_operator/client.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **gs.pterodactyl.com Custom Resources** — charts_pterodactyl_operator_crds_templates_crds_pterodactylaction_customresourcedefinition_pterodactylactions_gs_pterodactyl_com, charts_pterodactyl_operator_crds_templates_crds_pterodactylcontrol_customresourcedefinition_pterodactylcontrols_gs_pterodactyl_com, charts_pterodactyl_operator_crds_templates_crds_pterodactylgamecatalog_customresourcedefinition_pterodactylgamecatalogs_gs_pterodactyl_com, charts_pterodactyl_operator_crds_templates_crds_pterodactylnode_customresourcedefinition_pterodactylnodes_gs_pterodactyl_com, charts_pterodactyl_operator_crds_templates_crds_pterodactylserver_customresourcedefinition_pterodactylservers_gs_pterodactyl_com, charts_pterodactyl_operator_crds_templates_crds_pterodactylwebsocketrequest_customresourcedefinition_pterodactylwebsocketrequests_gs_pterodactyl_com [EXTRACTED 1.00]
- **Pterodactyl panel authentication split** — docs_pterodactyl_legacy_api_panel_contract, docs_pterodactyl_legacy_api_application_key, docs_pterodactyl_legacy_api_client_key, docs_pterodactyl_legacy_api_wings_node_token [EXTRACTED 1.00]
- **SolusVM exporter scrape to Mimir** — charts_solusvm_operator_templates_prometheus_exporter_deployment_deployment_exporter, charts_solusvm_operator_templates_prometheus_exporter_service_service_exporter, charts_solusvm_operator_templates_alloy_configmap_configmap_alloy_config, charts_solusvm_operator_templates_networkpolicy_networkpolicy_exporter [EXTRACTED 1.00]
- **VirtFusion control, node, and VM model** — charts_virtfusion_operator_crds_templates_crds_virtfusioncontrol_customresourcedefinition_virtfusioncontrols_vm_virtfusion_com, charts_virtfusion_operator_crds_templates_crds_virtfusionnode_customresourcedefinition_virtfusionnodes_vm_virtfusion_com, charts_virtfusion_operator_crds_templates_crds_virtfusionvm_customresourcedefinition_virtfusionvms_vm_virtfusion_com [EXTRACTED 1.00]
- **vm.proxmox.com Custom Resources** — charts_proxmox_operator_crds_templates_crds_firewallruleset_customresourcedefinition_firewallrulesets_vm_proxmox_com, charts_proxmox_operator_crds_templates_crds_proxmoxnode_customresourcedefinition_proxmoxnodes_vm_proxmox_com, charts_proxmox_operator_crds_templates_crds_proxmoxvm_customresourcedefinition_proxmoxvms_vm_proxmox_com, charts_proxmox_operator_crds_templates_crds_vmbackup_customresourcedefinition_vmbackups_vm_proxmox_com, charts_proxmox_operator_crds_templates_crds_vmsnapshot_customresourcedefinition_vmsnapshots_vm_proxmox_com, charts_proxmox_operator_crds_templates_crds_vncrequest_customresourcedefinition_vncrequests_vm_proxmox_com [EXTRACTED 1.00]
- **Provider lifecycle hook isolation** — docs_hooks_lifecycle_hook_scripts, docs_hooks_hook_job_namespace, docs_hooks_ssrf_hardened_egress, charts_virtfusion_operator_templates_hook_job_namespace_namespace_hook_jobs, charts_virtfusion_operator_templates_hook_job_networkpolicy_networkpolicy_hook_jobs [INFERRED 0.85]
- **Proxmox Operator Provider Runtime** — charts_proxmox_operator_templates_deployment_deployment_proxmox_operator, charts_proxmox_operator_templates_prometheus_exporter_deployment_deployment_proxmox_operator_exporter, charts_proxmox_operator_templates_prometheus_exporter_service_service_proxmox_operator_exporter, charts_proxmox_operator_templates_vnc_gateway_deployment_deployment_proxmox_operator_vnc_gateway, charts_proxmox_operator_templates_vnc_gateway_service_service_vnc_gateway, charts_proxmox_operator_templates_alloy_configmap_configmap_proxmox_operator_alloy_config [INFERRED 0.85]
- **Pterodactyl Operator Runtime** — charts_pterodactyl_operator_templates_deployment_deployment_pterodactyl_operator, charts_pterodactyl_operator_templates_console_service_service_pterodactyl_console, charts_pterodactyl_operator_templates_prometheus_exporter_service_deployment_pterodactyl_operator_exporter, charts_pterodactyl_operator_templates_prometheus_exporter_service_service_pterodactyl_operator_exporter, charts_pterodactyl_operator_templates_role_clusterrole_pterodactyl_operator, charts_pterodactyl_operator_templates_role_clusterrolebinding_pterodactyl_operator, charts_pterodactyl_operator_templates_alloy_configmap_configmap_pterodactyl_operator_alloy_config [INFERRED 0.85]
- **SolusVM Control and Node CRDs** — charts_solusvm_operator_chart_solusvm_operator, charts_solusvm_operator_crds_chart_solusvm_operator_crds, charts_solusvm_operator_crds_templates_crds_solusvmcontrol_customresourcedefinition_solusvmcontrols_vm_solusvm_com, charts_solusvm_operator_crds_templates_crds_solusvmnode_customresourcedefinition_solusvmnodes_vm_solusvm_com [INFERRED 0.85]
- **SolusVM operator workload and RBAC** — charts_solusvm_operator_templates_deployment_deployment_operator, charts_solusvm_operator_templates_role_clusterrole_role, charts_solusvm_operator_templates_rolebinding_clusterrolebinding_rb, charts_solusvm_operator_templates_hook_job_namespace_namespace_hook_jobs [INFERRED 0.85]
- **VirtFusion operator workload and RBAC** — charts_virtfusion_operator_templates_deployment_deployment_operator, charts_virtfusion_operator_templates_role_clusterrole_role, charts_virtfusion_operator_templates_rolebinding_clusterrolebinding_rb, charts_virtfusion_operator_templates_hook_job_namespace_namespace_hook_jobs [INFERRED 0.85]
- **Hook Job Namespace Sandbox** — charts_proxmox_operator_templates_deployment_deployment_proxmox_operator, charts_proxmox_operator_templates_hook_job_namespace_namespace_hook_jobs, charts_proxmox_operator_templates_hook_job_networkpolicy_networkpolicy_proxmox_operator_hook_jobs [INFERRED 0.95]

## Communities (102 total, 22 thin omitted)

### Community 0 - "virtfusion_operator/servers.py"
Cohesion: 0.13
Nodes (27): as_mapping(), built_stamp(), dns_servers_from_server(), add(), _extract_user_task_id(), _gateway_from_items(), _guest_install_ready(), ipv4_from_qty_result() (+19 more)

### Community 2 - "test_virtfusion_lifecycle.py"
Cohesion: 0.06
Nodes (51): clear_smtp_ruleset_cache(), client(), _core_missing_secret(), _k8s_node_control(), _KopfPatch, _run(), _spec(), test_build_never_sends_password_or_mail() (+43 more)

### Community 3 - "typing"
Cohesion: 0.05
Nodes (26): _discover_instances(), load_credentials(), SolusVMCredentials, _already_ready(), _drop_secrets(), _location_ids(), on_control_create(), on_control_update() (+18 more)

### Community 4 - "handlers.py"
Cohesion: 0.06
Nodes (22): _addresses_from_proxmox_spec(), _client_for_vm(), configure(), _get_client(), _handle_operation(), _is_transient_proxmox_error(), on_create(), on_update() (+14 more)

### Community 5 - "virtfusion_operator/handlers/vm.py"
Cohesion: 0.06
Nodes (37): _apply_user_api_build(), _auth_secret_present(), _build_client(), _configured_user_id(), _decode_secret_key(), _ensure_auth_secret_if_missing(), _guest_admin_user(), _handle_created() (+29 more)

### Community 6 - "virtfusion_operator/guest_userdata.py"
Cohesion: 0.07
Nodes (32): _as_list(), _as_mapping(), _dump(), instant_app_for_solusvm(), prepare_solusvm_user_data(), _sshd_password_login(), _as_list(), _as_mapping() (+24 more)

### Community 7 - "solusvm_operator/handlers/vm.py"
Cohesion: 0.09
Nodes (30): _assert_ipv4_assignment(), _build_client(), _decode_secret_key(), _expected_ipv4(), _fail_ipv4_mismatch(), _guest_password(), _handle_created(), _handle_operation() (+22 more)

### Community 8 - "test_solusvm_lifecycle.py"
Cohesion: 0.06
Nodes (33): _handler(), do_GET(), do_POST(), _read_json(), _send(), svm_http(), _SVMState, _KopfPatch (+25 more)

### Community 9 - "Patch"
Cohesion: 0.10
Nodes (22): SnapshotPhase, _check_in_flight_restore_task(), _check_in_flight_task(), _check_proxmox_task_status(), _create_snapshot(), _format_datetime_for_k8s(), _get_client(), _get_proxmoxvm_cr() (+14 more)

### Community 10 - "server.py"
Cohesion: 0.10
Nodes (32): dispatch_gameserver_hook(), power(), _accept_eula(), _allocation_status(), _egg_change_finished(), _egg_change_request(), _empty_recycle_bin(), _external_id_taken() (+24 more)

### Community 11 - "proxmox_operator/config.py"
Cohesion: 0.17
Nodes (8): _discover_proxmox_instances(), _find_credentials_for_url(), load_settings_from_env(), _normalize_url(), _parse_proxmox_url(), ProxmoxInstanceCredentials, test_load_settings_from_default_proxmox_url_env(), test_named_instance_env_wins_over_default_url()

### Community 12 - "solusvm_operator/servers.py"
Cohesion: 0.10
Nodes (23): unwrap_data(), SolusVMHTTPError, TransientError, create_server(), delete_ips(), delete_server(), is_not_found(), reinstall_server() (+15 more)

### Community 13 - "SolusVMClient"
Cohesion: 0.10
Nodes (10): as_mapping(), _check_response(), _format_error_body(), SolusVMClient, _ssh_key_owner_id(), add_ips(), change_hostname(), vnc_up() (+2 more)

### Community 14 - "VirtFusionClient"
Cohesion: 0.10
Nodes (6): VirtFusionClient, add_ipv4(), get_vnc_details(), modify_resources(), sync_extra_ipv4(), ensure_user()

### Community 15 - "firewall_reconcile.py"
Cohesion: 0.11
Nodes (15): _build_expected_proxmox_rule(), _build_firewall_rule_params(), _cleanup_firewall_resources(), _create_firewall_rule(), _create_firewall_rules_and_ipsets(), _create_ipset_with_prefixes(), _delete_firewall_rules(), _delete_ipset_entries() (+7 more)

### Community 16 - "bridge.py"
Cohesion: 0.05
Nodes (39): _get_backup_vzdump_post_delay_seconds(), _get_reconcile_interval(), _get_reconcile_interval(), _b64url(), _b64url_decode(), _bridge(), browser_to_wings(), wings_to_browser() (+31 more)

### Community 17 - "virtfusion_operator/smtp_outbound.py"
Cohesion: 0.10
Nodes (29): _apply_desired(), _current_applied_ids(), expanded_rules(), _firewall_data(), firewall_enabled(), merge_ruleset_ids(), pick_first_smtp_ruleset(), _positive_int() (+21 more)

### Community 18 - "PanelClient"
Cohesion: 0.22
Nodes (33): PanelClient, websocket_credentials(), create_allocation(), create_backup(), create_database(), create_schedule(), create_schedule_task(), _database_attributes() (+25 more)

### Community 19 - "pterodactyl_operator/servers.py"
Cohesion: 0.09
Nodes (34): allocation_from_server(), cores_from_cpu_percent(), create_placement_block(), create_server(), deployable_ids_from_payload(), docker_image(), _egg_record(), _egg_records() (+26 more)

### Community 20 - "catalog.py"
Cohesion: 0.11
Nodes (13): load_credentials(), PanelCredentials, is_permanent(), catalog_games(), catalog_is_current(), egg_requires_eula(), _flag(), on_catalog() (+5 more)

### Community 21 - "test_pterodactyl_client.py"
Cohesion: 0.05
Nodes (37): docker_image_choices(), UnsafePath, create_should_stop(), empty_required_keys(), choose_pinned_threads(), _AllocClient, _NestClient, _RecordingClient (+29 more)

### Community 22 - "node_handler.py"
Cohesion: 0.11
Nodes (21): build_inventory_from_cpuinfo(), _cpu_from_status(), CpuInventoryBrand, extract_cpuinfo(), _get_client(), get_proxmox_node_spec(), _handle_proxmoxnode_change(), infer_brand() (+13 more)

### Community 23 - "proxmox_operator/types.py"
Cohesion: 0.07
Nodes (33): AdditionalIPv4Address, CloudInitConfig, ConfigMapRef, FirewallRule, FirewallRuleSetRef, FirewallRuleSetStatus, FirewallTarget, LastDelete (+25 more)

### Community 24 - "backup_reconcile.py"
Cohesion: 0.15
Nodes (14): _create_backup(), _get_client(), _get_proxmoxvm_cr(), _get_vmbackup_cr(), _list_backups(), _newest_backup_volid_and_size(), _patch_status(), reconcile_vmbackup() (+6 more)

### Community 25 - "action.py"
Cohesion: 0.14
Nodes (17): action_already_finished(), _allocation(), _backup(), begin_action(), _cron(), _db(), _decode(), _encode() (+9 more)

### Community 26 - "VirtFusionHTTPError"
Cohesion: 0.11
Nodes (12): VirtFusionCredentials, TransientError, VirtFusionHTTPError, is_noop_power_failure(), _queue_fail_message(), delete_server(), is_not_found(), ensure_user_api_token() (+4 more)

### Community 27 - "CustomResourceDefinition virtfusionvms.vm.virtfusion.com"
Cohesion: 0.10
Nodes (23): CustomResourceDefinition solusvmvms.vm.solusvm.com, CustomResourceDefinition solusvmvncrequests.vm.solusvm.com, ClusterRole solusvm-operator-role, ClusterRoleBinding solusvm-operator-rb, CustomResourceDefinition virtfusioncontrols.vm.virtfusion.com, CustomResourceDefinition virtfusionnodes.vm.virtfusion.com, CustomResourceDefinition virtfusionvms.vm.virtfusion.com, CustomResourceDefinition virtfusionvncrequests.vm.virtfusion.com (+15 more)

### Community 28 - "logger"
Cohesion: 0.15
Nodes (6): logger(), _make_api_exception(), _node_cr(), _patch_k8s_client(), TestGetProxmoxNodeSpec, TestResolveProxmoxUrl

### Community 29 - "runner.py"
Cohesion: 0.23
Nodes (6): _hooks_map(), _provider_secret_sources(), _ProviderSecretSources, _script_for(), HookAddresses, VMHooks

### Community 30 - "pytest"
Cohesion: 0.14
Nodes (10): _handler(), test_execute_clones_when_name_absent(), test_execute_raises_when_name_lookup_hits_api_error(), test_execute_skips_clone_when_name_exists(), _make_api_exception(), test_get_proxmox_client_404_is_permanent(), test_get_proxmox_client_non_404_is_temporary(), test_reconcile_permanent_proxmox_error_does_not_retry() (+2 more)

### Community 31 - "test_egg_change.py"
Cohesion: 0.21
Nodes (18): Panel, _paths(), _run(), _spec(), test_egg_without_eula_starts_and_does_not_write_eula(), test_finished_reinstall_does_not_wipe_again(), test_fivem_to_minecraft_wipes_switches_reinstalls_and_accepts_eula(), test_neoforge_writes_eula_when_the_catalog_flag_is_false() (+10 more)

### Community 32 - "operations.py"
Cohesion: 0.06
Nodes (12): cleanup_cloudinit_iso(), DeleteOperationHandler, get_operation_handler(), OperationHandler, _parse_vmid_raw(), ReinstallOperationHandler, RestartOperationHandler, StartOperationHandler (+4 more)

### Community 33 - "run_hook_for_operation"
Cohesion: 0.15
Nodes (8): _annotations(), _count_active_jobs(), job_name(), _labels(), run_hook_for_operation(), _api_error(), TestJobName, TestRunHook

### Community 34 - "_vm_config_with_ips"
Cohesion: 0.19
Nodes (4): _host_cidr(), TestReconcileIpfilterIpsets, TestStaticIpfilterCidrs, _vm_config_with_ips()

### Community 35 - "_client"
Cohesion: 0.21
Nodes (18): _client(), _Response, _server(), _Session, test_client_call_reads_nr_external_id_from_the_application_api(), test_client_call_refuses_a_panel_server_without_nr_external_id(), test_create_allows_a_customer_hostname(), test_create_requires_nr_external_id() (+10 more)

### Community 36 - "_config_post_kwargs"
Cohesion: 0.24
Nodes (3): _config_post_kwargs(), TestConfigureCpu, TestConfigureNet

### Community 37 - "proxmox_operator/client.py"
Cohesion: 0.17
Nodes (6): _exception_message_chain(), _ipfilter_cidr_key(), _ipv6_filter_cidr(), _ipv6_ipfilter_cidr(), _snapshot_absent_on_delete(), _storage_content_absent_on_delete()

### Community 38 - "solusvm_operator/client.py"
Cohesion: 0.09
Nodes (10): _build_session(), _verify_ssl_from_env(), _build_session(), _verify_ssl_from_env(), _guest_ips(), main(), _ssh(), _ssh_probe() (+2 more)

### Community 39 - "unittest_mock"
Cohesion: 0.10
Nodes (4): TestCloudinitIsoCleanup, TestCloudinitIsoFilename, TestDeleteStorageContent, TestUploadIsoToStorage

### Community 40 - "CreateOperationHandler"
Cohesion: 0.08
Nodes (7): CreateOperationHandler, SecretData, _get_k8s_core_api(), parse_memory_to_mb(), read_auth_secret(), read_configmap(), read_secret()

### Community 41 - "default_project_id"
Cohesion: 0.23
Nodes (3): default_project_id(), DefaultProjectTests, _FakeClient

### Community 42 - "ProxmoxClient"
Cohesion: 0.21
Nodes (12): ProxmoxClient, client(), _patch_qemu_list(), test_find_vm_by_name_propagates_proxmox_error_from_connection(), test_find_vm_by_name_raises_on_invalid_list_entry(), test_find_vm_by_name_raises_on_transport_error(), test_find_vm_by_name_returns_none_when_absent(), test_find_vm_by_name_returns_vmid_when_found() (+4 more)

### Community 43 - "files.py"
Cohesion: 0.20
Nodes (19): _file_transfer(), chmod_file(), compress_files(), copy_file(), create_folder(), decompress_file(), delete_files(), download_file() (+11 more)

### Community 44 - "solusvm_operator/sshkeys.py"
Cohesion: 0.15
Nodes (8): ensure_ssh_keys(), _fingerprint(), _index_keys(), _key_body(), _key_name(), _load_key_index(), EnsureSSHKeysTests, _FakeClient

### Community 45 - "TestGenerateCidataIso"
Cohesion: 0.07
Nodes (5): ProxmoxSettings, client(), TestGenerateCidataIso, client(), client()

### Community 46 - "test_noderings_hooks.py"
Cohesion: 0.18
Nodes (5): last_hook_patch(), on_hook_job_event(), test_last_hook_patch_shape(), test_sample_hook_script_is_valid_python3(), TestWatcher

### Community 47 - "Any"
Cohesion: 0.16
Nodes (18): _apply_server_status(), compute_resource_id_from_server(), _ip_addresses_block(), parse_dns_servers(), add(), parse_ipv4(), parse_ipv4_gateway(), parse_ipv4_ordered() (+10 more)

### Community 48 - "reconcile_vm_smtp_outbound_block"
Cohesion: 0.22
Nodes (13): _create_smtp_rule(), delete_non_smtp_firewall_rules(), _delete_smtp_rules(), _existing_smtp_ports(), is_smtp_outbound_rule(), _list_rules(), reconcile_vm_smtp_outbound_block(), smtp_outbound_comment() (+5 more)

### Community 49 - "_FakeSVM"
Cohesion: 0.31
Nodes (3): _FakeSVM, test_add_ips_does_not_block_on_pending_task(), test_wait_for_extra_ipv4_succeeds_while_task_pending()

### Community 50 - "load_hook_settings"
Cohesion: 0.15
Nodes (7): _bool(), HookSettings, _int(), load_hook_settings(), _provider_secret_name(), TestJobNamespace, TestProviderSecretsSettings

### Community 51 - "TemplateCompiler"
Cohesion: 0.13
Nodes (3): compile_if_template(), TemplateCompilationError, TemplateCompiler

### Community 52 - "test_virtfusion_ipv4.py"
Cohesion: 0.08
Nodes (18): _extract_queue_id(), ipv4_addresses_from_server(), power_action(), remove_ipv4(), sync_remove_ipv4(), wait_until_server_built(), _SeqClient, test_extract_queue_id_ignores_list_data() (+10 more)

### Community 53 - "pterodactyl_operator/client.py"
Cohesion: 0.26
Nodes (7): _body_external_id(), _detail(), is_noderings_server(), _only_noderings(), _require_record(), _server_target(), test_prefix_is_nr_only()

### Community 54 - "Hook job namespace"
Cohesion: 0.15
Nodes (12): Deployment solusvm-operator, Namespace solusvm-operator hook jobs, NetworkPolicy solusvm-operator-hook-jobs, NetworkPolicy solusvm-operator-operator, Deployment virtfusion-operator, Namespace virtfusion-operator hook jobs, NetworkPolicy virtfusion-operator-hook-jobs, NetworkPolicy virtfusion-operator-operator (+4 more)

### Community 56 - "_reconcile_firewall_rules_timer"
Cohesion: 0.15
Nodes (9): _get_client(), _get_target_vms_by_proxmox_url(), _get_vms_for_proxmox_url(), on_firewall_ruleset_delete(), _reconcile_firewall_rules_timer(), reconcile_firewall_ruleset(), _reconcile_ipsets_timer(), _rules_match() (+1 more)

### Community 58 - "choose_egg"
Cohesion: 0.15
Nodes (10): choose_egg(), _edit_distance(), EggMatchError, InstallFailed, _normalize_name(), test_choose_egg_allows_two_character_edits(), test_choose_egg_allows_two_extra_characters(), test_choose_egg_exact_name_ignores_case() (+2 more)

### Community 59 - "virtfusion_operator/handlers/node.py"
Cohesion: 0.06
Nodes (27): LastHookStatus, _discover_instances(), load_credentials(), _parse_user_id(), _user_fields(), _already_ready(), on_control_create(), on_control_update() (+19 more)

### Community 60 - "test_pterodactyl_bridge_grants.py"
Cohesion: 0.15
Nodes (4): _client_key(), test_expired_session(), test_missing_key_cannot_mint(), test_require_single_replica_rejects_scale()

### Community 61 - "Proxmox VM Operator"
Cohesion: 0.17
Nodes (7): Apache License 2.0 Contributions, Poetry Development Setup, Full VM Lifecycle, Harbor and GHCR Operator Images, Multi-Instance Proxmox Support, Proxmox VM Operator, Supported Version 0.1.x

### Community 62 - "Deployment proxmox-operator"
Cohesion: 0.07
Nodes (37): proxmox-operator-crds Chart 0.1.9, Platform Cluster CRD Install, vm.proxmox.com API Group, CustomResourceDefinition firewallrulesets.vm.proxmox.com, CustomResourceDefinition proxmoxnodes.vm.proxmox.com, CustomResourceDefinition proxmoxvms.vm.proxmox.com, CustomResourceDefinition vmbackups.vm.proxmox.com, CustomResourceDefinition vmsnapshots.vm.proxmox.com (+29 more)

### Community 63 - "ConfigMap proxmox-operator-alloy-config"
Cohesion: 0.21
Nodes (12): ConfigMap proxmox-operator-alloy-config, NetworkPolicy proxmox-operator-exporter, Deployment proxmox-operator-exporter, Service proxmox-operator-exporter, Alloy Remote Write to Mimir, Prometheus PVE Exporter, ConfigMap pterodactyl-operator-alloy-config, Deployment pterodactyl-operator-exporter (+4 more)

### Community 65 - "_check_response"
Cohesion: 0.18
Nodes (6): _check_response(), build_server_with_userdata(), _insecure_session(), mint_user_api_token(), poll_user_task(), VirtFusionUserClient

### Community 66 - "dispatch_hook"
Cohesion: 0.22
Nodes (6): _as_str_list(), dispatch_hook(), merge_addresses(), safe_run_hook(), HookConcurrencyError, TestSafeRunHook

### Community 67 - "NetworkConfig"
Cohesion: 0.18
Nodes (10): DiskConfig, NetworkConfig, Operation, ProxmoxNodeRef, VMConfig, create_spec(), make_handler(), TestNetworkConfigValidation (+2 more)

### Community 68 - "Deployment pterodactyl-operator"
Cohesion: 0.33
Nodes (12): CustomResourceDefinition pterodactylactions.gs.pterodactyl.com, CustomResourceDefinition pterodactylcontrols.gs.pterodactyl.com, CustomResourceDefinition pterodactylgamecatalogs.gs.pterodactyl.com, CustomResourceDefinition pterodactylnodes.gs.pterodactyl.com, CustomResourceDefinition pterodactylservers.gs.pterodactyl.com, CustomResourceDefinition pterodactylwebsocketrequests.gs.pterodactyl.com, Service pterodactyl-console, Deployment pterodactyl-operator (+4 more)

### Community 70 - "_get_k8s_client"
Cohesion: 0.17
Nodes (4): _get_k8s_client(), _get_proxmox_client_from_vm_ref(), _is_transient_proxmox_error(), reconcile_vnc_request()

### Community 71 - "VirtFusion Operator Helm Chart"
Cohesion: 0.18
Nodes (9): Grafana Alloy Helm dependency, VirtFusion Operator Helm Chart, virtfusion-operator-crds Helm Chart, virtfusion-operator-crds enabled, ConfigMap virtfusion-operator-alloy-config, NetworkPolicy virtfusion-operator-exporter, Deployment virtfusion-operator-exporter, Service virtfusion-operator-exporter (+1 more)

### Community 75 - "Pterodactyl Application API"
Cohesion: 0.22
Nodes (10): Pterodactyl Application API, Pterodactyl application key, Pterodactyl Client API, Pterodactyl client key, Pterodactyl create server body, Pterodactyl list pagination, Pterodactyl panel contract, Pterodactyl websocket console (+2 more)

### Community 76 - "on_vnc_request"
Cohesion: 0.17
Nodes (5): management_host(), on_vnc_request(), _resolve_server(), build_ws_url(), get_vnc_password()

### Community 77 - "PanelError"
Cohesion: 0.24
Nodes (5): PanelError, _DeployClient, test_create_placement_requires_deployable_before_maintenance(), test_list_eggs_skips_a_nest_whose_eggs_fail(), application()

### Community 79 - "resolve_vmid_str_from_vm_cr"
Cohesion: 0.33
Nodes (6): resolve_vmid_str_from_vm_cr(), test_resolve_empty_returns_none(), test_resolve_falls_back_to_spec_vmId(), test_resolve_legacy_status_vmid(), test_resolve_prefers_status_vmId(), test_resolve_strips_whitespace()

### Community 81 - "java_minecraft_server"
Cohesion: 0.32
Nodes (3): java_minecraft_server(), test_bedrock_and_proxies_do_not_use_eula_txt(), test_java_minecraft_variants_need_eula_txt()

### Community 82 - "proxmox-operator Chart 0.1.9"
Cohesion: 0.40
Nodes (5): Grafana Alloy Subchart, proxmox-operator Chart 0.1.9, proxmox-operator-crds Subchart, proxmox-operator Helm Chart, proxmox-operator-crds Helm Chart

### Community 83 - "normalize_prefix"
Cohesion: 0.21
Nodes (6): netmask_from_prefix(), normalize_prefix(), addresses_from_status(), _event_json(), _job_spec(), test_addresses_from_status_and_merge()

### Community 84 - "projects.py"
Cohesion: 0.36
Nodes (4): _owner_id(), _pick_project_id(), _projects_for_user(), resolve_project()

### Community 85 - "virtfusion_operator/users.py"
Cohesion: 0.29
Nodes (4): ext_relation_id_for_api(), test_non_uuid_label_hashes(), test_numeric_passthrough(), test_uuid_is_stable_positive_int()

### Community 90 - "Deployment virtfusion-operator-vnc-gateway"
Cohesion: 0.40
Nodes (4): Deployment virtfusion-operator-vnc-gateway, NetworkPolicy virtfusion-operator-vnc-gateway, Service vnc-gateway, ServiceMonitor virtfusion-operator-vnc-gateway

### Community 93 - "test_virtfusion_isolation.py"
Cohesion: 0.60
Nodes (3): _read_tree(), test_proxmox_operator_never_mentions_virtfusion(), test_virtfusion_operator_never_imports_proxmox()

### Community 94 - "Deployment solusvm-operator-exporter"
Cohesion: 0.50
Nodes (4): ConfigMap solusvm-operator-alloy-config, NetworkPolicy solusvm-operator-exporter, Deployment solusvm-operator-exporter, Service solusvm-operator-exporter

### Community 95 - "Deployment solusvm-operator-vnc-gateway"
Cohesion: 0.50
Nodes (4): Deployment solusvm-operator-vnc-gateway, NetworkPolicy solusvm-operator-vnc-gateway, Service vnc-gateway, ServiceMonitor solusvm-operator-vnc-gateway

## Knowledge Gaps
- **41 isolated node(s):** `proxmox-operator`, `In-memory Proxmox credentials cache`, `ClusterRoleBinding solusvm-operator-rb`, `ClusterRoleBinding virtfusion-operator-rb`, `FirewallRuleSet example-web-firewall` (+36 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 792 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **22 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Logger` connect `Logger` to `typing`, `handlers.py`, `virtfusion_operator/handlers/vm.py`, `solusvm_operator/handlers/vm.py`, `Patch`, `server.py`, `firewall_reconcile.py`, `virtfusion_operator/smtp_outbound.py`, `catalog.py`, `node_handler.py`, `backup_reconcile.py`, `test_egg_change.py`, `operations.py`, `run_hook_for_operation`, `CreateOperationHandler`, `test_noderings_hooks.py`, `reconcile_vm_smtp_outbound_block`, `_reconcile_firewall_rules_timer`, `virtfusion_operator/handlers/node.py`, `dispatch_hook`, `_get_k8s_client`, `on_vnc_request`?**
  _High betweenness centrality (0.080) - this node is a cross-community bridge._
- **Are the 39 inferred relationships involving `ProxmoxClient` (e.g. with `_create_backup()` and `_list_backups()`) actually correct?**
  _`ProxmoxClient` has 39 INFERRED edges - model-reasoned connections that need verification._
- **What connects `proxmox-operator`, `In-memory Proxmox credentials cache`, `ClusterRoleBinding solusvm-operator-rb` to the rest of the system?**
  _41 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `virtfusion_operator/servers.py` be split into smaller, more focused modules?**
  _Cohesion score 0.13333333333333333 - nodes in this community are weakly interconnected._
- **Why does `ProxmoxClient` connect `ProxmoxClient` to `cloudinit_iso_filename`, `ProxmoxError`, `operations.py`, `handlers.py`, `proxmox_operator/client.py`, `_get_k8s_client`, `Any`, `.configure_vm`, `Patch`, `TestGenerateCidataIso`, `firewall_reconcile.py`, `node_handler.py`, `proxmox_operator/types.py`, `backup_reconcile.py`, `_reconcile_firewall_rules_timer`, `.reconcile_ipfilter_ipsets`?**
  _High betweenness centrality (0.060) - this node is a cross-community bridge._
- **Are the 78 inferred relationships involving `PanelClient` (e.g. with `websocket_credentials()` and `create_allocation()`) actually correct?**
  _`PanelClient` has 78 INFERRED edges - model-reasoned connections that need verification._
- **Should `ProxmoxError` be split into smaller, more focused modules?**
  _Cohesion score 0.0858974358974359 - nodes in this community are weakly interconnected._