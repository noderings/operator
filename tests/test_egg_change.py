"""Egg change wipes the server root, switches the egg, reinstalls, and starts."""

from __future__ import annotations

from typing import Any

import pytest

from pterodactyl_operator.files import wipe_server_files
from pterodactyl_operator.handlers import server as server_handler
from pterodactyl_operator.servers import update_server_startup


class Logger:
    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None

    def error(self, *args: Any, **kwargs: Any) -> None:
        return None

    def debug(self, *args: Any, **kwargs: Any) -> None:
        return None

    def exception(self, *args: Any, **kwargs: Any) -> None:
        return None


class Patch:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}


class Panel:
    def __init__(
        self,
        *,
        offline_after_stop: bool = True,
        eula: bool = True,
        install_status: str | None = None,
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.state = "running"
        self.offline_after_stop = offline_after_stop
        self.installed = True
        self.install_reads = 0
        self.install_status = install_status

    def client_api(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        body = kwargs.get("json_body")
        self.calls.append(
            ("client", method, path, body, kwargs.get("params"), kwargs.get("raw_body"))
        )
        if path.endswith("/power"):
            signal = (body or {}).get("signal")
            if signal == "stop" and self.offline_after_stop:
                self.state = "offline"
            if signal == "kill":
                self.state = "offline"
            if signal == "start":
                self.state = "running"
            return 204, {}
        if path.endswith("/resources"):
            return 200, {"attributes": {"current_state": self.state}}
        if path.endswith("/files/list"):
            return 200, {
                "data": [
                    {"attributes": {"name": "server.cfg"}},
                    {"attributes": {"name": "resources"}},
                ]
            }
        if path.endswith("/files/delete") or path.endswith("/files/write"):
            return 204, {}
        if path.endswith("/settings/reinstall"):
            self.installed = True
            return 204, {}
        return 200, {}

    def application(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        self.calls.append(("app", method, path, kwargs.get("json_body")))
        if "/eggs/" in path:
            return 200, {
                "attributes": {
                    "id": 53,
                    "startup": "java -jar {{SERVER_JARFILE}}",
                    "docker_image": "ghcr.io/pterodactyl/yolks:java_21",
                    "relationships": {
                        "variables": {
                            "data": [
                                {
                                    "attributes": {
                                        "env_variable": "SERVER_JARFILE",
                                        "default_value": "server.jar",
                                    }
                                }
                            ]
                        }
                    },
                }
            }
        if path.endswith("/startup"):
            return 200, {"attributes": {}}
        if method == "GET" and "/servers/" in path:
            self.install_reads += 1
            # The first read is still "installing" so the handler does not start
            # the game before the installer has been observed.
            status = (
                self.install_status
                if self.install_status is not None
                else ("installing" if self.install_reads == 1 else None)
            )
            return 200, {"attributes": {"id": 9, "status": status, "identifier": "abc"}}
        return 200, {"attributes": {}}


def _spec(
    *,
    accept_eula: bool,
    requested_at: str = "2026-10-04T00:00:00Z",
    egg_name: str = "",
    environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": "testnoderings3",
        "externalId": "NR-dea71370-1770-439c-8493-7c46138ae2bf",
        "nestId": 1,
        "eggId": 53,
        "eggName": egg_name,
        "environment": environment or {"SERVER_JARFILE": "server.jar"},
        "acceptEula": accept_eula,
        "operation": {"type": "egg_changed", "requestedAt": requested_at},
    }


def _run(
    panel: Panel,
    status: dict[str, Any] | None = None,
    *,
    accept_eula: bool = True,
    egg_name: str = "",
    environment: dict[str, Any] | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> Patch:
    if monkeypatch is not None:
        monkeypatch.setattr(
            "pterodactyl_operator.servers.time.sleep", lambda *_args, **_kwargs: None
        )
    patch = Patch()
    server_handler._handle(
        {
            "spec": _spec(accept_eula=accept_eula, egg_name=egg_name, environment=environment),
            "status": status
            or {
                "phase": "Ready",
                "serverId": 9,
                "identifier": "abc",
                "allocation": {"ip": "203.0.113.10", "port": 20094},
            },
        },
        patch,  # type: ignore[arg-type]
        Logger(),  # type: ignore[arg-type]
        {"namespace": "default", "generation": 1},
    )
    return patch


def _paths(panel: Panel) -> list[str]:
    return [call[2] for call in panel.calls]


def test_fivem_to_minecraft_wipes_switches_reinstalls_and_accepts_eula(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = Panel(offline_after_stop=False)
    monkeypatch.setattr(server_handler, "_client", lambda: panel)
    patch = _run(panel, accept_eula=True, monkeypatch=monkeypatch)

    paths = _paths(panel)
    assert paths.index("/servers/abc/power") < paths.index("/servers/abc/files/list")
    assert "/servers/abc/files/delete" in paths
    delete = next(call for call in panel.calls if call[2].endswith("/files/delete"))
    assert delete[3] == {"root": "/", "files": ["server.cfg", "resources"]}
    startup = next(call for call in panel.calls if call[2].endswith("/startup"))
    assert startup[1] == "PATCH"
    assert startup[3]["egg"] == 53
    assert startup[3]["image"] == "ghcr.io/pterodactyl/yolks:java_21"
    assert startup[3]["startup"] == "java -jar {{SERVER_JARFILE}}"
    assert startup[3]["environment"]["SERVER_JARFILE"] == "server.jar"
    assert startup[3]["skip_scripts"] is False
    assert paths.index("/servers/9/startup") < paths.index("/servers/abc/settings/reinstall")
    assert paths.index("/servers/abc/settings/reinstall") < paths.index(
        "/servers/abc/files/trash/empty"
    )
    empty = next(call for call in panel.calls if call[2].endswith("/files/trash/empty"))
    assert empty[1] == "POST"
    assert paths.index("/servers/abc/files/trash/empty") < len(paths) - 1
    written = next(call for call in panel.calls if call[2].endswith("/files/write"))
    assert written[5] == b"eula=true\n"
    assert panel.calls[-1][3] == {"signal": "start"}
    assert "kill" in [
        call[3].get("signal") for call in panel.calls if call[2].endswith("/power") and call[3]
    ]
    assert patch.status["phase"] == "Ready"
    assert patch.status["observedRequestedAt"] == "2026-10-04T00:00:00Z"
    assert patch.status["eggChangeStep"] == ""


def test_neoforge_writes_eula_when_the_catalog_flag_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = Panel()
    monkeypatch.setattr(server_handler, "_client", lambda: panel)
    _run(
        panel,
        accept_eula=False,
        egg_name="NeoForge",
        environment={"MC_VERSION": "26.3", "NEOFORGE_VERSION": "26.3.0.48-beta"},
        monkeypatch=monkeypatch,
    )
    assert any(
        call[5] == b"eula=true\n" for call in panel.calls if call[2].endswith("/files/write")
    )


def test_egg_without_eula_starts_and_does_not_write_eula(monkeypatch: pytest.MonkeyPatch) -> None:
    panel = Panel()
    monkeypatch.setattr(server_handler, "_client", lambda: panel)
    _run(panel, accept_eula=False, monkeypatch=monkeypatch)
    assert not any(call[2].endswith("/files/write") for call in panel.calls)
    assert panel.calls[-1][2].endswith("/power")
    assert panel.calls[-1][3] == {"signal": "start"}


def test_retry_after_reinstalling_does_not_delete_files(monkeypatch: pytest.MonkeyPatch) -> None:
    panel = Panel()
    monkeypatch.setattr(server_handler, "_client", lambda: panel)
    _run(
        panel,
        {
            "phase": "Pending",
            "serverId": 9,
            "identifier": "abc",
            "eggChangeStep": "reinstalling",
            "eggChangeRequest": "2026-10-04T00:00:00Z",
            "allocation": {"ip": "203.0.113.10", "port": 20094},
        },
        accept_eula=True,
    )
    assert not any(call[2].endswith("/files/delete") for call in panel.calls)
    assert not any(call[2].endswith("/startup") for call in panel.calls)
    assert not any(call[2].endswith("/settings/reinstall") for call in panel.calls)
    assert any(
        call[5] == b"eula=true\n" for call in panel.calls if call[2].endswith("/files/write")
    )


def test_reinstall_failed_ends_the_game_change(monkeypatch: pytest.MonkeyPatch) -> None:
    panel = Panel(install_status="reinstall_failed")
    monkeypatch.setattr(server_handler, "_client", lambda: panel)
    patch = _run(panel, accept_eula=True, monkeypatch=monkeypatch)

    assert patch.status["phase"] == "Error"
    assert patch.status["message"] == server_handler.INSTALL_FAILED_MESSAGE
    assert patch.status["observedRequestedAt"] == "2026-10-04T00:00:00Z"
    assert patch.status["eggChangeStep"] == ""
    assert any(call[2].endswith("/settings/reinstall") for call in panel.calls)
    assert not any(
        call[2].endswith("/power") and (call[3] or {}).get("signal") == "start"
        for call in panel.calls
    )
    assert panel.install_reads == 1


def test_same_request_after_error_does_not_call_the_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise AssertionError("panel should not be called")

    monkeypatch.setattr(server_handler, "_client", boom)
    patch = _run(
        Panel(),
        {
            "phase": "Error",
            "serverId": 9,
            "identifier": "abc",
            "observedRequestedAt": "2026-10-04T00:00:00Z",
        },
    )
    assert patch.status == {}


def test_same_request_after_ready_does_not_call_the_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise AssertionError("panel should not be called")

    monkeypatch.setattr(server_handler, "_client", boom)
    patch = _run(
        Panel(),
        {
            "phase": "Ready",
            "serverId": 9,
            "identifier": "abc",
            "observedRequestedAt": "2026-10-04T00:00:00Z",
        },
    )
    assert patch.status == {}


def test_wipe_of_an_empty_root_does_not_delete() -> None:
    class Empty(Panel):
        def client_api(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
            self.calls.append(
                ("client", method, path, kwargs.get("json_body"), kwargs.get("params"), None)
            )
            return 200, {"data": []}

    panel = Empty()
    wipe_server_files(panel, "abc")  # type: ignore[arg-type]
    assert not any(call[2].endswith("/files/delete") for call in panel.calls)


def test_reinstall_wait_ignores_the_already_installed_status_until_installing_is_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pterodactyl_operator.servers import wait_until_reinstall_finished

    panel = Panel()
    monkeypatch.setattr("pterodactyl_operator.servers.time.sleep", lambda *_args, **_kwargs: None)
    wait_until_reinstall_finished(panel, 9, poll=0.0, start_grace=30)  # type: ignore[arg-type]
    assert panel.install_reads >= 2


def test_reinstall_deletes_files_and_empties_the_recycle_bin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = Panel(offline_after_stop=False)
    monkeypatch.setattr(server_handler, "_client", lambda: panel)
    monkeypatch.setattr("pterodactyl_operator.servers.time.sleep", lambda *_args, **_kwargs: None)
    patch = Patch()
    server_handler._handle(
        {
            "spec": {
                **_spec(accept_eula=True),
                "operation": {"type": "reinstall", "requestedAt": "2026-10-04T00:00:00Z"},
            },
            "status": {
                "phase": "Ready",
                "serverId": 9,
                "identifier": "abc",
                "allocation": {"ip": "203.0.113.10", "port": 20094},
            },
        },
        patch,  # type: ignore[arg-type]
        Logger(),  # type: ignore[arg-type]
        {"namespace": "default", "generation": 1},
    )

    paths = _paths(panel)
    assert paths.index("/servers/abc/power") < paths.index("/servers/abc/files/list")
    delete = next(call for call in panel.calls if call[2].endswith("/files/delete"))
    assert delete[3] == {"root": "/", "files": ["server.cfg", "resources"]}
    trash = [index for index, path in enumerate(paths) if path.endswith("/files/trash/empty")]
    reinstall_at = paths.index("/servers/abc/settings/reinstall")
    assert paths.index("/servers/abc/files/delete") < trash[0] < reinstall_at < trash[-1]
    assert reinstall_at < paths.index("/servers/abc/files/write")
    assert any(
        call[5] == b"eula=true\n" for call in panel.calls if call[2].endswith("/files/write")
    )
    assert not any(call[2].endswith("/startup") for call in panel.calls)
    assert patch.status["phase"] == "Ready"
    assert patch.status["observedRequestedAt"] == "2026-10-04T00:00:00Z"


def test_reinstall_failure_still_drops_the_old_files(monkeypatch: pytest.MonkeyPatch) -> None:
    panel = Panel(install_status="reinstall_failed")
    monkeypatch.setattr(server_handler, "_client", lambda: panel)
    patch = Patch()
    server_handler._handle(
        {
            "spec": {
                **_spec(accept_eula=True),
                "operation": {"type": "reinstall", "requestedAt": "2026-10-04T00:00:00Z"},
            },
            "status": {"phase": "Ready", "serverId": 9, "identifier": "abc"},
        },
        patch,  # type: ignore[arg-type]
        Logger(),  # type: ignore[arg-type]
        {"namespace": "default", "generation": 1},
    )

    paths = _paths(panel)
    assert paths.index("/servers/abc/files/delete") < paths.index("/servers/abc/files/trash/empty")
    assert paths.index("/servers/abc/files/trash/empty") < paths.index(
        "/servers/abc/settings/reinstall"
    )
    assert patch.status["phase"] == "Error"
    assert patch.status["message"] == server_handler.INSTALL_FAILED_MESSAGE
    assert not any(call[2].endswith("/files/write") for call in panel.calls)


def test_finished_reinstall_does_not_wipe_again(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise AssertionError("panel should not be called")

    monkeypatch.setattr(server_handler, "_client", boom)
    patch = Patch()
    server_handler._handle(
        {
            "spec": {
                **_spec(accept_eula=True),
                "operation": {"type": "reinstall", "requestedAt": "2026-10-04T00:00:00Z"},
            },
            "status": {
                "phase": "Ready",
                "serverId": 9,
                "identifier": "abc",
                "observedRequestedAt": "2026-10-04T00:00:00Z",
            },
        },
        patch,  # type: ignore[arg-type]
        Logger(),  # type: ignore[arg-type]
        {"namespace": "default", "generation": 1},
    )
    assert patch.status == {}


def test_update_server_startup_patches_the_application_api() -> None:
    panel = Panel()
    update_server_startup(panel, 9, 53, "java", "java:21", {"SERVER_JARFILE": "server.jar"})  # type: ignore[arg-type]
    method, path, body = panel.calls[0][1], panel.calls[0][2], panel.calls[0][3]
    assert method == "PATCH"
    assert path == "/servers/9/startup"
    assert body["egg"] == 53
    assert body["skip_scripts"] is False
