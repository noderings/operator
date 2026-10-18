"""Unit tests for path safety and egg environment mapping. No panel calls."""

from typing import Any, cast

from noderings_hooks.runner import gameserver_hook_env
from pterodactyl_operator.control import (
    _database_attributes,
    docker_image_choices,
    list_schedules,
    update_schedule,
    update_schedule_task,
)
from pterodactyl_operator.handlers.action import _schedule
from pterodactyl_operator.reconcile import create_should_stop, power_signal
from pterodactyl_operator.files import UnsafePath, reject_unsafe
from pterodactyl_operator.handlers.catalog import catalog_games, catalog_is_current, variable_defs
from pterodactyl_operator.client import PanelClient, PanelError
from pterodactyl_operator.servers import (
    EggMatchError,
    choose_egg,
    list_eggs,
    choose_pinned_threads,
    cores_from_cpu_percent,
    create_placement_block,
    docker_image,
    environment_from_egg,
    merged_environment,
    allocation_from_server,
    newest_free_allocation,
    parse_thread_set,
)


def test_reject_parent_directory() -> None:
    try:
        reject_unsafe("/nr/../escape.txt")
    except UnsafePath:
        return
    raise AssertionError("parent directory was accepted")


def test_reject_encoded_parent_directory() -> None:
    for path in ("%2e%2e/escape", "%252e%252e/escape", "foo/%2e%2e/bar"):
        try:
            reject_unsafe(path)
        except UnsafePath:
            continue
        raise AssertionError(path)


def test_reject_backslash_and_nul() -> None:
    for path in ("C:\\windows", "ok\x00txt"):
        try:
            reject_unsafe(path)
        except UnsafePath:
            continue
        raise AssertionError(path)


def test_allow_server_root_path() -> None:
    assert reject_unsafe("/nr-file-sim/hello.txt") == "/nr-file-sim/hello.txt"


def test_environment_uses_egg_defaults() -> None:
    egg = {
        "relationships": {
            "variables": {
                "data": [
                    {"attributes": {"env_variable": "MAX_USERS", "default_value": "100"}},
                    {"attributes": {"env_variable": "EMPTY", "default_value": None}},
                ]
            }
        }
    }
    assert environment_from_egg(egg) == {"MAX_USERS": "100", "EMPTY": ""}
    assert merged_environment(egg, {})["MAX_USERS"] == "100"
    assert merged_environment(egg, {"MAX_USERS": ""})["MAX_USERS"] == "100"
    assert merged_environment(egg, {"MAX_USERS": "50"})["MAX_USERS"] == "50"


def test_docker_image_string() -> None:
    assert docker_image({"docker_image": "ghcr.io/example:tag"}) == "ghcr.io/example:tag"


class _AllocClient:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def application(self, method, path, *, json_body=None, params=None):
        assert method == "GET"
        return 200, {
            "data": [{"attributes": row} for row in self.rows],
            "meta": {"pagination": {"total_pages": 1}},
        }


def test_database_password_is_nested_in_attributes() -> None:
    payload = {
        "attributes": {
            "id": "abc",
            "name": "s1_nr",
            "relationships": {"password": {"attributes": {"password": "secret"}}},
        }
    }
    attrs = _database_attributes(payload)
    assert attrs["password"] == "secret"
    assert "relationships" not in attrs


def test_create_stops_when_required_environment_is_empty() -> None:
    reason = create_should_stop(
        {"environment": {"SERVER_JARFILE": ""}, "requiredEnvironment": ["SERVER_JARFILE"]},
        {},
    )
    assert reason and "SERVER_JARFILE" in reason


def test_create_skips_when_already_ready() -> None:
    assert create_should_stop({}, {"phase": "Ready", "serverId": 4}) == ""


def test_create_stops_in_maintenance() -> None:
    assert create_should_stop({"maintenance": True}, {}) == "node is in maintenance"


class _DeployClient:
    def __init__(self, ids: list[int], *, maintenance: bool = False, empty: bool = False) -> None:
        self.ids = ids
        self.maintenance = maintenance
        self.empty = empty

    def application(self, method, path, *, json_body=None, params=None):
        assert method == "GET"
        if path == "/nodes/deployable":
            if self.empty:
                raise PanelError(400, "No nodes are available")
            return 200, {
                "data": [{"attributes": {"id": node_id}} for node_id in self.ids],
                "meta": {"pagination": {"total_pages": 1}},
            }
        assert path == "/nodes/7"
        return 200, {"attributes": {"id": 7, "maintenance_mode": self.maintenance}}


def test_create_placement_requires_deployable_before_maintenance() -> None:
    client = _DeployClient([7])
    assert create_placement_block(client, 7, 2048, 10240) is None  # type: ignore[arg-type]
    blocked = _DeployClient([8])
    assert create_placement_block(blocked, 7, 2048, 10240) == "node is not deployable"  # type: ignore[arg-type]
    maintained = _DeployClient([7], maintenance=True)
    assert create_placement_block(maintained, 7, 2048, 10240) == "node is in maintenance"  # type: ignore[arg-type]
    none = _DeployClient([], empty=True)
    assert create_placement_block(none, 7, 2048, 10240) == "node is not deployable"  # type: ignore[arg-type]


def test_power_signal_mapping() -> None:
    assert power_signal("restarted") == "restart"
    assert power_signal("created") is None


def test_gameserver_hook_env_omits_vm_variables() -> None:
    names = {
        item.name for item in gameserver_hook_env("created", "NR-1", "1", "192.168.1.111", "40199")
    }
    assert names == {
        "NODERINGS_HOOK",
        "NODERINGS_EVENT_JSON",
        "NODERINGS_GAMESERVER_NAME",
        "NODERINGS_GAMESERVER_ID",
        "NODERINGS_GAME_IP",
        "NODERINGS_GAME_PORT",
    }


def test_allocation_from_server_uses_primary() -> None:
    server = {
        "allocation": 749,
        "relationships": {
            "allocations": {
                "data": [
                    {"attributes": {"id": 10, "ip": "10.0.0.1", "port": 2000, "alias": ""}},
                    {
                        "attributes": {
                            "id": 749,
                            "ip": "192.168.1.111",
                            "port": 40198,
                            "alias": "game",
                        }
                    },
                ]
            }
        },
    }
    picked = allocation_from_server(server)
    assert picked["id"] == 749
    assert picked["ip"] == "192.168.1.111"
    assert picked["port"] == 40198


def test_newest_free_allocation_is_highest_id() -> None:
    client = _AllocClient(
        [
            {"id": 10, "assigned": False, "ip": "192.168.1.111", "port": 2000},
            {"id": 40, "assigned": True, "ip": "192.168.1.111", "port": 2001},
            {"id": 25, "assigned": False, "ip": "192.168.1.111", "port": 2002},
        ]
    )
    picked = newest_free_allocation(client, 1)  # type: ignore[arg-type]
    assert picked["id"] == 25


def test_catalog_games_marks_eula_from_feature_tag_or_startup_prompt() -> None:
    games = catalog_games(
        [
            {
                "id": 53,
                "nest_id": 11,
                "nest_name": "Modded Minecraf",
                "name": "Fabirc",
                "panel_egg": {
                    "feature_tags": [],
                    "config": {"startup": {"userInteraction": ["Go to eula.txt for more info."]}},
                },
            },
            {
                "id": 1,
                "nest_id": 2,
                "nest_name": "Voice",
                "name": "Mumbl",
                "feature_tags": ["eula"],
            },
            {
                "id": 9,
                "nest_id": 4,
                "nest_name": "Modded Minecraft",
                "name": "Fabric",
                "feature_tags": ["java"],
                "config": {"startup": {"userInteraction": ["Change the port"]}},
            },
        ]
    )
    assert [(game["eggId"], game["eula"]) for game in games] == [
        (53, True),
        (1, True),
        (9, False),
    ]


def test_catalog_games_keeps_nest_and_egg_ids() -> None:
    games = catalog_games(
        [
            {"id": 5, "nest_id": 1, "nest_name": "Minecraft", "name": "Paper"},
            {"id": 5, "nest_id": 1, "nest_name": "Minecraft", "name": "Paper"},
            {"id": 13, "nest_id": 3, "nest_name": "Voice Servers", "name": "Mumble Server"},
        ]
    )
    assert games == [
        {"nestId": 1, "nestName": "Minecraft", "eggId": 5, "eggName": "Paper", "eula": False},
        {
            "nestId": 3,
            "nestName": "Voice Servers",
            "eggId": 13,
            "eggName": "Mumble Server",
            "eula": False,
        },
    ]


class _NestClient:
    def __init__(
        self, nests: list[dict], eggs_by_nest: dict[int, list[dict]] | None = None
    ) -> None:
        self.nests = nests
        self.eggs_by_nest = eggs_by_nest or {}
        self.calls: list[tuple[str, dict]] = []

    def application(self, method: str, path: str, json_body=None, params=None):  # type: ignore[no-untyped-def]
        self.calls.append((path, dict(params or {})))
        if path == "/nests":
            return 200, {
                "data": [{"attributes": nest} for nest in self.nests],
                "meta": {"pagination": {"total_pages": 1}},
            }
        nest_id = int(path.split("/")[2])
        return 200, {
            "data": [{"attributes": egg} for egg in self.eggs_by_nest.get(nest_id, [])],
            "meta": {"pagination": {"total_pages": 1}},
        }


def test_docker_image_choices_keeps_each_egg_image_once() -> None:
    rows = docker_image_choices(
        {
            "Java 25": "ghcr.io/pterodactyl/yolks:java_25",
            "Java 21": "ghcr.io/pterodactyl/yolks:java_21",
            "Java 25 again": "ghcr.io/pterodactyl/yolks:java_25",
            "": "",
        }
    )
    assert rows == [
        {"name": "Java 25", "image": "ghcr.io/pterodactyl/yolks:java_25"},
        {"name": "Java 21", "image": "ghcr.io/pterodactyl/yolks:java_21"},
    ]
    assert docker_image_choices(None) == []


def test_variable_defs_reads_the_egg_relationship() -> None:
    rows = variable_defs(
        {
            "relationships": {
                "variables": {
                    "data": [
                        {
                            "attributes": {
                                "env_variable": "PROJECT_ID",
                                "name": "Modpack Project ID",
                                "description": "Modrinth project",
                                "default_value": None,
                                "rules": "required|string|max:20",
                                "user_viewable": 1,
                                "user_editable": 1,
                            }
                        },
                        {
                            "attributes": {
                                "env_variable": "SERVER_JARFILE",
                                "name": "Jar File",
                                "description": "",
                                "default_value": "server.jar",
                                "rules": "required|string",
                                "user_viewable": True,
                                "user_editable": 0,
                            }
                        },
                        {"attributes": {"name": "missing env"}},
                    ]
                }
            }
        }
    )
    assert rows == [
        {
            "envVariable": "PROJECT_ID",
            "name": "Modpack Project ID",
            "description": "Modrinth project",
            "defaultValue": "",
            "rules": "required|string|max:20",
            "userViewable": True,
            "userEditable": True,
            "required": True,
        },
        {
            "envVariable": "SERVER_JARFILE",
            "name": "Jar File",
            "description": "",
            "defaultValue": "server.jar",
            "rules": "required|string",
            "userViewable": True,
            "userEditable": False,
            "required": True,
        },
    ]


def test_list_eggs_uses_the_included_eggs() -> None:
    client = _NestClient(
        [
            {
                "id": 1,
                "name": "Minecraft",
                "relationships": {
                    "eggs": {
                        "data": [
                            {
                                "attributes": {
                                    "id": 5,
                                    "name": "Paper",
                                    "nest": 1,
                                    "relationships": {
                                        "variables": {
                                            "data": [
                                                {
                                                    "attributes": {
                                                        "env_variable": "SERVER_JARFILE",
                                                        "name": "Jar File",
                                                        "rules": "required|string",
                                                        "default_value": "server.jar",
                                                        "user_viewable": True,
                                                        "user_editable": 0,
                                                    }
                                                }
                                            ]
                                        }
                                    },
                                }
                            }
                        ]
                    }
                },
            }
        ]
    )
    eggs = list_eggs(client)  # type: ignore[arg-type]
    assert client.calls == [("/nests", {"per_page": 100, "page": 1, "include": "eggs.variables"})]
    assert eggs[0]["id"] == 5
    assert eggs[0]["name"] == "Paper"
    games = catalog_games(eggs)
    assert games == [
        {
            "nestId": 1,
            "nestName": "Minecraft",
            "eggId": 5,
            "eggName": "Paper",
            "eula": False,
            "variables": [
                {
                    "envVariable": "SERVER_JARFILE",
                    "name": "Jar File",
                    "description": "",
                    "defaultValue": "server.jar",
                    "rules": "required|string",
                    "userViewable": True,
                    "userEditable": False,
                    "required": True,
                }
            ],
        }
    ]


def test_list_eggs_lists_each_nest_once_when_variables_are_missing() -> None:
    client = _NestClient(
        [
            {
                "id": 1,
                "name": "Minecraft",
                "relationships": {
                    "eggs": {
                        "data": [
                            {"attributes": {"id": 5, "name": "Paper", "nest": 1}},
                            {"attributes": {"id": 50, "name": "FiveM", "nest": 1}},
                        ]
                    }
                },
            }
        ],
        {
            1: [
                {
                    "id": 5,
                    "name": "Paper",
                    "nest": 1,
                    "relationships": {"variables": {"data": []}},
                },
                {
                    "id": 50,
                    "name": "FiveM",
                    "nest": 1,
                    "relationships": {"variables": {"data": []}},
                },
            ]
        },
    )
    eggs = list_eggs(client)  # type: ignore[arg-type]
    assert [path for path, _params in client.calls] == ["/nests", "/nests/1/eggs"]
    assert client.calls[1][1]["include"] == "variables"
    assert [egg["id"] for egg in eggs] == [5, 50]


def test_list_eggs_skips_a_nest_whose_eggs_fail() -> None:
    class _Client(_NestClient):
        def application(self, method: str, path: str, json_body=None, params=None):  # type: ignore[no-untyped-def]
            include = (params or {}).get("include")
            if path == "/nests" and include == "eggs.variables":
                self.calls.append((path, dict(params or {})))
                raise PanelError(500, "JsonException: Syntax error")
            if path == "/nests/18/eggs":
                self.calls.append((path, dict(params or {})))
                raise PanelError(500, "JsonException: Syntax error")
            return super().application(method, path, json_body=json_body, params=params)

    client = _Client(
        [
            {"id": 9, "name": "Minecraft Java"},
            {"id": 18, "name": "Minecraft"},
            {"id": 10, "name": "Minecraft Bedrock"},
        ],
        {
            9: [
                {"id": 29, "name": "Paper", "nest": 9, "relationships": {"variables": {"data": []}}}
            ],
            10: [
                {
                    "id": 31,
                    "name": "Vanilla Bedrock",
                    "nest": 10,
                    "relationships": {"variables": {"data": []}},
                }
            ],
        },
    )
    eggs = list_eggs(client)  # type: ignore[arg-type]
    assert [egg["id"] for egg in eggs] == [29, 31]
    assert [path for path, _params in client.calls] == [
        "/nests",
        "/nests",
        "/nests/9/eggs",
        "/nests/18/eggs",
        "/nests/10/eggs",
    ]


def test_list_eggs_skips_one_broken_egg_record() -> None:
    client = _NestClient(
        [
            {
                "id": 1,
                "name": "Minecraft",
                "relationships": {
                    "eggs": {
                        "data": [
                            {"attributes": {"name": "Broken"}},
                            {
                                "attributes": {
                                    "id": 5,
                                    "name": "Paper",
                                    "nest": 1,
                                    "relationships": {"variables": {"data": []}},
                                }
                            },
                        ]
                    }
                },
            }
        ]
    )
    eggs = list_eggs(client)  # type: ignore[arg-type]
    assert [egg["id"] for egg in eggs] == [5]


def test_list_eggs_falls_back_when_include_is_missing() -> None:
    client = _NestClient(
        [{"id": 2, "name": "Voice"}],
        {2: [{"id": 13, "name": "Mumble", "nest": 2}]},
    )
    eggs = list_eggs(client)  # type: ignore[arg-type]
    assert [path for path, _params in client.calls] == ["/nests", "/nests/2/eggs"]
    assert client.calls[1][1]["include"] == "variables"
    assert eggs == [{"id": 13, "nest_id": 2, "nest_name": "Voice", "name": "Mumble"}]


def test_catalog_is_current_only_after_a_ready_list() -> None:
    assert catalog_is_current(None) is False
    assert catalog_is_current({"phase": "Error", "games": []}) is False
    assert catalog_is_current({"phase": "Ready", "games": []}) is True


def test_choose_egg_exact_name_ignores_case() -> None:
    eggs = [
        {"id": 13, "nest_id": 3, "name": "Mumble Server"},
        {"id": 4, "nest_id": 1, "name": "Paper"},
    ]
    assert choose_egg(eggs, "mumble server") == (3, 13)


def test_choose_egg_allows_two_character_edits() -> None:
    eggs = [{"id": 9, "nest_id": 2, "name": "Garrys Mod"}]
    assert choose_egg(eggs, "Garry's Mod") == (2, 9)


def test_choose_egg_allows_two_extra_characters() -> None:
    eggs = [{"id": 4, "nest_id": 1, "name": "PaperMC"}]
    assert choose_egg(eggs, "Paper") == (1, 4)


def test_choose_egg_rejects_a_longer_name() -> None:
    eggs = [{"id": 4, "nest_id": 1, "name": "Vanilla Minecraft"}]
    try:
        choose_egg(eggs, "Paper")
    except EggMatchError:
        return
    raise AssertionError("Paper matched Vanilla Minecraft")


def test_parse_thread_set_accepts_lists_and_ranges() -> None:
    assert parse_thread_set(None) == set()
    assert parse_thread_set("") == set()
    assert parse_thread_set("0,1") == {0, 1}
    assert parse_thread_set("0-3") == {0, 1, 2, 3}
    assert parse_thread_set("0-1,4") == {0, 1, 4}


def test_two_core_plan_pins_the_first_free_pair() -> None:
    assert cores_from_cpu_percent(200) == 2
    assert cores_from_cpu_percent(400) == 4
    assert choose_pinned_threads(8, {0, 1}, 2) == "2,3"


def test_pinning_skips_cores_already_taken() -> None:
    assert choose_pinned_threads(4, {0, 2}, 2) == "1,3"


def test_pinning_fails_when_the_node_is_full() -> None:
    try:
        choose_pinned_threads(2, {0}, 2)
    except RuntimeError:
        return
    raise AssertionError("a full node accepted another dedicated server")


class _ScheduleClient:
    def client_api(self, method, path, **_kwargs):
        assert method == "GET"
        assert path.endswith("/schedules")
        return 200, {
            "data": [
                {
                    "attributes": {
                        "id": 4,
                        "name": "Nightly",
                        "cron": {
                            "minute": "0",
                            "hour": "4",
                            "day_of_month": "*",
                            "month": "*",
                            "day_of_week": "*",
                        },
                        "is_active": True,
                        "only_when_online": True,
                        "is_processing": False,
                        "last_run_at": "2026-09-29T03:00:00+00:00",
                        "next_run_at": "2026-09-30T04:00:00+00:00",
                    },
                    "relationships": {
                        "tasks": {
                            "data": [
                                {
                                    "attributes": {
                                        "id": 9,
                                        "sequence_id": 1,
                                        "action": "command",
                                        "payload": "say hi",
                                        "time_offset": 15,
                                        "continue_on_failure": True,
                                    }
                                }
                            ]
                        }
                    },
                }
            ]
        }


def test_list_schedules_keeps_task_relationships() -> None:
    rows = list_schedules(cast(PanelClient, _ScheduleClient()), "abc")
    assert rows[0]["relationships"]["tasks"]["data"][0]["attributes"]["payload"] == "say hi"


def test_schedule_maps_tasks() -> None:
    mapped = _schedule(list_schedules(cast(PanelClient, _ScheduleClient()), "abc")[0])
    assert mapped["name"] == "Nightly"
    assert mapped["minute"] == "0"
    assert mapped["onlyWhenOnline"] is True
    assert mapped["lastRunAt"] == "2026-09-29T03:00:00+00:00"
    assert mapped["nextRunAt"] == "2026-09-30T04:00:00+00:00"
    assert mapped["isProcessing"] is False
    assert mapped["tasks"] == [
        {
            "id": "9",
            "sequenceId": 1,
            "action": "command",
            "payload": "say hi",
            "timeOffset": 15,
            "continueOnFailure": True,
        }
    ]


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def client_api(self, method, path, json_body=None, **_kwargs):
        self.calls.append((method, path, json_body or {}))
        return 200, {
            "attributes": {
                "id": 4,
                "name": (json_body or {}).get("name", "Nightly"),
                "action": (json_body or {}).get("action", "command"),
                "payload": (json_body or {}).get("payload", ""),
                "time_offset": (json_body or {}).get("time_offset", 0),
                "continue_on_failure": (json_body or {}).get("continue_on_failure", False),
                "sequence_id": (json_body or {}).get("sequence_id", 1),
                "cron": {
                    "minute": (json_body or {}).get("minute", "*"),
                    "hour": (json_body or {}).get("hour", "*"),
                    "day_of_month": "*",
                    "month": "*",
                    "day_of_week": "*",
                },
                "is_active": (json_body or {}).get("is_active", True),
                "only_when_online": (json_body or {}).get("only_when_online", False),
            }
        }


def test_update_schedule_posts_the_cron() -> None:
    client = _RecordingClient()
    update_schedule(
        cast(PanelClient, client),
        "abc",
        4,
        name="Morning",
        minute="15",
        hour="3",
        day_of_month="*",
        month="*",
        day_of_week="*",
        only_when_online=True,
        is_active=False,
    )
    method, path, body = client.calls[0]
    assert method == "POST"
    assert path.endswith("/schedules/4")
    assert body["name"] == "Morning"
    assert body["minute"] == "15"
    assert body["is_active"] is False
    assert body["only_when_online"] is True


def test_update_task_omits_sequence_until_reordered() -> None:
    client = _RecordingClient()
    update_schedule_task(
        cast(PanelClient, client),
        "abc",
        4,
        9,
        action="command",
        payload="say hi",
        time_offset=5,
        continue_on_failure=True,
    )
    assert "sequence_id" not in client.calls[0][2]
    update_schedule_task(
        cast(PanelClient, client),
        "abc",
        4,
        9,
        action="command",
        payload="say hi",
        time_offset=5,
        continue_on_failure=True,
        sequence_id=2,
    )
    assert client.calls[1][1].endswith("/schedules/4/tasks/9")
    assert client.calls[1][2]["sequence_id"] == 2


def test_restore_backup_sends_truncate() -> None:
    from pterodactyl_operator.control import restore_backup
    from pterodactyl_operator.handlers import action as action_handler

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def client_api(self, method: str, path: str, **kwargs):
            self.calls.append((method, path, kwargs.get("json_body")))
            return 204, {}

    client = Client()
    restore_backup(client, "abc", "bak", truncate=True)  # type: ignore[arg-type]
    restore_backup(client, "abc", "bak")  # type: ignore[arg-type]
    assert client.calls[0] == ("POST", "/servers/abc/backups/bak/restore", {"truncate": True})
    assert client.calls[1][2] == {"truncate": False}

    seen: dict[str, object] = {}

    def fake(_client, identifier: str, backup_id: str, *, truncate: bool = False) -> None:
        seen["identifier"] = identifier
        seen["backup_id"] = backup_id
        seen["truncate"] = truncate

    original = action_handler.control.restore_backup
    original_client = action_handler._client
    action_handler.control.restore_backup = cast(Any, fake)
    action_handler._client = cast(Any, lambda: None)
    try:
        action_handler._run("abc", "restore_backup", {"backupUuid": "bak", "truncate": True})
    finally:
        action_handler.control.restore_backup = original
        action_handler._client = original_client
    assert seen == {"identifier": "abc", "backup_id": "bak", "truncate": True}


def test_choose_egg_rejects_a_tie() -> None:
    eggs = [
        {"id": 1, "nest_id": 1, "name": "PaperA"},
        {"id": 2, "nest_id": 1, "name": "PaperB"},
    ]
    try:
        choose_egg(eggs, "Paper")
    except EggMatchError:
        return
    raise AssertionError("two close eggs were accepted")
