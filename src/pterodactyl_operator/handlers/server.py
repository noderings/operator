"""PterodactylServer: create on the newest free allocation, then power and delete."""

from __future__ import annotations

import threading
import time
from typing import Any

import kopf

from ..client import PanelClient, PanelError
from ..config import load_credentials
from ..eula import java_minecraft_server
from ..panel import panel_client as _client
from ..constants import (
    GROUP,
    OP_CREATED,
    PHASE_ERROR,
    PHASE_PENDING,
    PHASE_READY,
    SERVER_PLURAL,
    VERSION,
)
from ..control import power, resources
from ..files import empty_recycle_bin, wipe_server_files, write_file
from ..errors import is_permanent
from ..reconcile import create_should_stop, power_signal
from ..servers import (
    EggMatchError,
    choose_egg,
    allocation_from_server,
    create_server,
    delete_server,
    docker_image,
    get_egg,
    get_server,
    get_server_by_external_id,
    list_eggs,
    merged_environment,
    newest_free_allocation,
    create_placement_block,
    pinned_threads_for_node,
    update_server_startup,
    wait_until_installed,
    InstallFailed,
    wait_until_reinstall_finished,
)
from ..status import set_status
from noderings_hooks.runner import dispatch_gameserver_hook

_POWER_OPS = frozenset({"started", "stopped", "restarted", "kill"})
INSTALL_FAILED_MESSAGE = (
    "The panel could not install this game. You can reinstall it or change games."
)
INSTALL_TIMEOUT_MESSAGE = "The panel install did not finish. You can reinstall it or change games."


def _gameserver_id(external_id: str) -> str:
    """Game server uuid. The panel external id is that uuid with an NR- prefix."""
    text = (external_id or "").strip()
    if text.upper().startswith("NR-"):
        return text[3:]
    return text


_create_guard = threading.Lock()
_create_locks: dict[str, threading.Lock] = {}


def _spec(body: dict[str, Any]) -> dict[str, Any]:
    return body.get("spec") or {}


def _status(body: dict[str, Any]) -> dict[str, Any]:
    return body.get("status") or {}


def _allocation_status(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "ip": row.get("ip") or "",
        "port": int(row.get("port") or 0),
        "alias": row.get("alias") or "",
    }


def _handle(
    body: dict[str, Any], patch: kopf.Patch, logger: kopf.Logger, meta: dict[str, Any]
) -> None:
    spec = _spec(body)
    status = _status(body)
    operation = ((spec.get("operation") or {}).get("type") or OP_CREATED).lower()
    requested_at = (spec.get("operation") or {}).get("requestedAt")
    if operation in _POWER_OPS:
        _handle_power(spec, status, patch, logger, meta, operation, requested_at)
        return
    if operation in {"suspended", "unsuspend"}:
        _handle_suspend(status, patch, operation)
        return
    if operation == "reinstall":
        _handle_reinstall(spec, status, patch, logger, meta)
        return
    if operation == "egg_changed":
        _handle_egg_changed(spec, status, patch, logger, meta)
        return
    _handle_created(spec, status, patch, logger, meta)


def _external_id_taken(exc: PanelError) -> bool:
    detail = exc.detail.casefold()
    return "external id" in detail and "taken" in detail


def _create_lock(external_id: str) -> threading.Lock:
    """One panel create at a time for an order. A second handler waits, then adopts."""
    key = external_id.strip() or "-"
    with _create_guard:
        lock = _create_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _create_locks[key] = lock
        return lock


def _server_for_external_id(
    client: PanelClient, external_id: str, attempts: int = 6
) -> dict[str, Any] | None:
    """The winner of a create race may commit a moment after the loser sees the unique error."""
    for attempt in range(attempts):
        existing = get_server_by_external_id(client, external_id)
        if existing:
            return existing
        if attempt + 1 < attempts:
            time.sleep(0.4 * (attempt + 1))
    return None


def _needs_eula_file(spec: dict[str, Any]) -> bool:
    """Java Minecraft always gets eula.txt, even when the catalog flag is false."""
    if spec.get("acceptEula"):
        return True
    environment = spec.get("environment") if isinstance(spec.get("environment"), dict) else {}
    return java_minecraft_server(str(spec.get("eggName") or ""), environment)


def _start_on_completion(spec: dict[str, Any]) -> bool:
    """Hold the first boot when we still have to write eula.txt."""
    if _needs_eula_file(spec):
        return False
    return bool(spec.get("startOnCompletion", True))


def _accept_eula(client: PanelClient, spec: dict[str, Any], identifier: str | None) -> None:
    """Write eula.txt and start. Java Minecraft exits if this line is missing."""
    if not _needs_eula_file(spec):
        return
    if not identifier:
        raise kopf.TemporaryError("eula file needs a panel identifier", delay=10)
    try:
        write_file(client, str(identifier), "/eula.txt", b"eula=true\n")
        power(client, str(identifier), "start")
    except PanelError as exc:
        raise kopf.TemporaryError(exc.detail, delay=15) from exc


def _mark_created(
    client: PanelClient,
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    meta: dict[str, Any],
    node_id: int,
    created: dict[str, Any],
    allocation: dict[str, Any],
) -> None:
    claimed = _allocation_status(allocation) if allocation.get("id") else {}
    server_id = int(created["id"])
    identifier = created.get("identifier")
    set_status(
        patch,
        phase=PHASE_PENDING,
        message="",
        server_id=server_id,
        uuid=created.get("uuid"),
        identifier=identifier,
        node_id=node_id,
        allocation=claimed,
    )
    installed = wait_until_installed(client, server_id)
    identifier = installed.get("identifier") or identifier
    _accept_eula(client, spec, identifier)
    set_status(
        patch,
        phase=PHASE_READY,
        message="",
        server_id=server_id,
        uuid=installed.get("uuid") or created.get("uuid"),
        identifier=identifier,
        node_id=node_id,
        allocation=claimed,
    )
    dispatch_gameserver_hook(
        hooks=spec.get("hooks"),
        op_type="created",
        meta=meta,
        status=status,
        logger=logger,
        name=str(spec.get("name") or ""),
        gameserver_id=_gameserver_id(str(spec.get("externalId") or "")),
        game_ip=str(claimed.get("ip") or ""),
        game_port=str(claimed.get("port") or ""),
    )


def _handle_created(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    meta: dict[str, Any],
) -> None:
    stop = create_should_stop(spec, status)
    if stop == "":
        return
    if stop:
        set_status(patch, phase=PHASE_ERROR, message=stop)
        return
    client = _client()
    node_id = int(spec["nodeId"])
    user_id = int(spec.get("panelUserId") or 0)
    if user_id <= 0:
        user_id = load_credentials().panel_user_id
    external_id = str(spec.get("externalId") or "")
    try:
        with _create_lock(external_id):
            if not status.get("serverId"):
                existing = get_server_by_external_id(client, external_id)
                if existing:
                    created = existing
                    allocation = allocation_from_server(existing)
                else:
                    nest_id = int(spec.get("nestId") or 0)
                    egg_id = int(spec.get("eggId") or 0)
                    if nest_id <= 0 or egg_id <= 0:
                        nest_id, egg_id = choose_egg(
                            list_eggs(client), str(spec.get("eggName") or "")
                        )
                    memory = int(spec["limits"]["memory"])
                    disk = int(spec["limits"]["disk"])
                    block = create_placement_block(client, node_id, memory, disk)
                    if block:
                        set_status(patch, phase=PHASE_ERROR, message=block)
                        return
                    allocation = newest_free_allocation(client, node_id)
                    cpu = int(spec["limits"]["cpu"])
                    threads = None
                    if spec.get("dedicated"):
                        threads = pinned_threads_for_node(client, node_id, cpu)
                    try:
                        created = create_server(
                            client,
                            name=str(spec["name"]),
                            user_id=user_id,
                            nest_id=nest_id,
                            egg_id=egg_id,
                            allocation_id=int(allocation["id"]),
                            external_id=external_id,
                            memory=int(spec["limits"]["memory"]),
                            disk=int(spec["limits"]["disk"]),
                            cpu=cpu,
                            threads=threads,
                            databases=int(spec["featureLimits"]["databases"]),
                            backups=int(spec["featureLimits"]["backups"]),
                            extra_allocations=max(int(spec["featureLimits"]["allocations"]) - 1, 0),
                            start_on_completion=_start_on_completion(spec),
                            environment={
                                str(k): str(v) for k, v in (spec.get("environment") or {}).items()
                            },
                            startup=spec.get("startup") or None,
                            image=spec.get("dockerImage") or None,
                        )
                    except PanelError as exc:
                        if not _external_id_taken(exc):
                            raise
                        # Another handler already created this order. Adopt it. Do not
                        # publish phase Error: the watcher would void the purchase and stop.
                        existing = _server_for_external_id(client, external_id)
                        if not existing:
                            raise kopf.TemporaryError(exc.detail, delay=10) from exc
                        created = existing
                        allocation = allocation_from_server(existing)
                claimed = _allocation_status(allocation)
                set_status(
                    patch,
                    phase=PHASE_PENDING,
                    message="",
                    server_id=int(created["id"]),
                    uuid=created.get("uuid"),
                    identifier=created.get("identifier"),
                    node_id=node_id,
                    allocation=claimed,
                )
                server_id = int(created["id"])
                identifier = created.get("identifier")
            else:
                server_id = int(status["serverId"])
                identifier = status.get("identifier")
                claimed = status.get("allocation") or {}
        installed = wait_until_installed(client, server_id)
        identifier = installed.get("identifier") or identifier
        _accept_eula(client, spec, identifier)
        set_status(
            patch,
            phase=PHASE_READY,
            message="",
            server_id=server_id,
            uuid=installed.get("uuid") or status.get("uuid"),
            identifier=identifier,
            node_id=node_id,
            allocation=claimed,
        )
        dispatch_gameserver_hook(
            hooks=spec.get("hooks"),
            op_type="created",
            meta=meta,
            status=status,
            logger=logger,
            name=str(spec.get("name") or ""),
            gameserver_id=_gameserver_id(str(spec.get("externalId") or "")),
            game_ip=str(claimed.get("ip") or ""),
            game_port=str(claimed.get("port") or ""),
        )
    except EggMatchError as exc:
        set_status(patch, phase=PHASE_ERROR, message=str(exc))
        return
    except PanelError as exc:
        if _external_id_taken(exc):
            existing = _server_for_external_id(client, external_id)
            if existing:
                _mark_created(
                    client,
                    spec,
                    status,
                    patch,
                    logger,
                    meta,
                    node_id,
                    existing,
                    allocation_from_server(existing),
                )
                return
            raise kopf.TemporaryError(exc.detail, delay=10) from exc
        if status.get("serverId"):
            try:
                delete_server(client, int(status["serverId"]))
            except PanelError:
                logger.warning("panel delete after create failure also failed")
        if is_permanent(exc):
            set_status(patch, phase=PHASE_ERROR, message=exc.detail)
            return
        raise kopf.TemporaryError(exc.detail, delay=15) from exc


def _handle_power(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    meta: dict[str, Any],
    operation: str,
    requested_at: str | None,
) -> None:
    if requested_at and status.get("observedRequestedAt") == requested_at:
        return
    identifier = status.get("identifier")
    signal = power_signal(operation)
    if not identifier or not signal:
        set_status(patch, phase=PHASE_ERROR, message="power requires an installed server")
        return
    power(_client(), str(identifier), signal)
    set_status(
        patch, phase=PHASE_READY, observed_requested_at=requested_at, identifier=str(identifier)
    )
    dispatch_gameserver_hook(
        hooks=spec.get("hooks"),
        op_type=operation,
        meta=meta,
        status=status,
        logger=logger,
        name=str(spec.get("name") or ""),
        gameserver_id=_gameserver_id(str(spec.get("externalId") or "")),
        game_ip=str((status.get("allocation") or {}).get("ip") or ""),
        game_port=str((status.get("allocation") or {}).get("port") or ""),
    )


def _handle_suspend(status: dict[str, Any], patch: kopf.Patch, operation: str) -> None:
    server_id = status.get("serverId")
    if not server_id:
        set_status(patch, phase=PHASE_ERROR, message="suspend requires a panel server id")
        return
    path = "unsuspend" if operation == "unsuspend" else "suspend"
    _client().application("POST", f"/servers/{int(server_id)}/{path}")
    set_status(patch, phase=PHASE_READY, server_id=int(server_id), message=path)


def _power_state(client: PanelClient, identifier: str) -> str:
    try:
        attrs = resources(client, identifier)
    except PanelError as exc:
        raise kopf.TemporaryError(exc.detail, delay=15) from exc
    return str(attrs.get("current_state") or "").lower()


def _wait_until_offline(client: PanelClient, identifier: str) -> None:
    deadline = time.monotonic() + 120
    while True:
        state = _power_state(client, identifier)
        if state in {"offline", "stopped"}:
            return
        if time.monotonic() > deadline:
            raise kopf.TemporaryError("the server did not stop", delay=15)
        time.sleep(2)


def _stop_for_wipe(client: PanelClient, identifier: str) -> None:
    """Stop, and kill if the process is still running. Wings recreates files under a live process."""
    try:
        power(client, identifier, "stop")
    except PanelError:
        # Already offline, or the panel rejected a stop. Kill covers a live process.
        pass
    if _power_state(client, identifier) in {"offline", "stopped"}:
        return
    try:
        power(client, identifier, "kill")
    except PanelError as exc:
        raise kopf.TemporaryError(exc.detail, delay=15) from exc
    _wait_until_offline(client, identifier)


def _egg_change_request(spec: dict[str, Any]) -> str:
    operation = spec.get("operation") or {}
    return str(operation.get("requestedAt") or "")


def _handle_egg_changed(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    meta: dict[str, Any],
) -> None:
    """Wipe the game directory, switch the egg, reinstall, and start. Same server, same address."""
    request = _egg_change_request(spec)
    if _egg_change_finished(status, request):
        return
    server_id = status.get("serverId")
    identifier = str(status.get("identifier") or "")
    if not server_id or not identifier:
        raise kopf.PermanentError("egg change requires an existing panel server")
    step_request = str(status.get("eggChangeRequest") or "")
    step = str(status.get("eggChangeStep") or "") if step_request == request else ""
    client = _client()
    if step in {"", "wiping"}:
        set_status(
            patch,
            phase=PHASE_PENDING,
            identifier=identifier,
            message="Replacing the game",
            egg_change_step="wiping",
            egg_change_request=request,
        )
        _stop_for_wipe(client, identifier)
        try:
            wipe_server_files(client, identifier)
        except PanelError as exc:
            raise kopf.TemporaryError(exc.detail, delay=30) from exc
        step = "updating"
        set_status(
            patch,
            phase=PHASE_PENDING,
            identifier=identifier,
            message="Switching the game",
            egg_change_step="updating",
            egg_change_request=request,
        )
    if step == "updating":
        nest_id = int(spec.get("nestId") or 0)
        egg_id = int(spec.get("eggId") or 0)
        if nest_id <= 0 or egg_id <= 0:
            raise kopf.PermanentError("egg change requires a nest and an egg")
        try:
            egg = get_egg(client, nest_id, egg_id)
            startup = str(spec.get("startup") or "").strip() or str(egg.get("startup") or "")
            image = str(spec.get("dockerImage") or "").strip() or docker_image(egg)
        except PanelError as exc:
            raise kopf.TemporaryError(exc.detail, delay=30) from exc
        except RuntimeError as exc:
            raise kopf.PermanentError(str(exc)) from exc
        overrides = spec.get("environment") if isinstance(spec.get("environment"), dict) else {}
        environment = merged_environment(egg, overrides)
        try:
            update_server_startup(client, int(server_id), egg_id, startup, image, environment)
            client.client_api("POST", f"/servers/{identifier}/settings/reinstall")
            wait_until_reinstall_finished(client, int(server_id))
        except PanelError as exc:
            raise kopf.TemporaryError(exc.detail, delay=30) from exc
        except InstallFailed:
            _fail_egg_change(patch, identifier, request, INSTALL_FAILED_MESSAGE)
            return
        except TimeoutError:
            _fail_egg_change(patch, identifier, request, INSTALL_TIMEOUT_MESSAGE)
            return
        except RuntimeError as exc:
            raise kopf.TemporaryError(str(exc), delay=30) from exc
        step = "reinstalling"
        set_status(
            patch,
            phase=PHASE_PENDING,
            identifier=identifier,
            message="Starting the new game",
            egg_change_step="reinstalling",
            egg_change_request=request,
        )
    if step == "reinstalling":
        _empty_recycle_bin(client, identifier, logger)
        if _needs_eula_file(spec):
            _accept_eula(client, spec, identifier)
        else:
            try:
                power(client, identifier, "start")
            except PanelError as exc:
                raise kopf.TemporaryError(exc.detail, delay=15) from exc
        set_status(
            patch,
            phase=PHASE_READY,
            identifier=identifier,
            server_id=int(server_id),
            message="Ready",
            observed_requested_at=request,
            egg_change_step="",
            egg_change_request=request,
        )
        dispatch_gameserver_hook(
            hooks=spec.get("hooks"),
            op_type="reinstalled",
            meta=meta,
            status=status,
            logger=logger,
            name=str(spec.get("name") or ""),
            gameserver_id=_gameserver_id(str(spec.get("externalId") or "")),
            game_ip=str((status.get("allocation") or {}).get("ip") or ""),
            game_port=str((status.get("allocation") or {}).get("port") or ""),
        )
        return
    raise kopf.PermanentError(f"unknown egg change step {step}")


def _egg_change_finished(status: dict[str, Any], request: str) -> bool:
    """Ready and Error both end this requestedAt. A later change uses a new timestamp."""
    return bool(
        request
        and str(status.get("observedRequestedAt") or "") == request
        and str(status.get("phase") or "") in {PHASE_READY, PHASE_ERROR}
    )


def _fail_egg_change(patch: kopf.Patch, identifier: str, request: str, message: str) -> None:
    set_status(
        patch,
        phase=PHASE_ERROR,
        identifier=identifier,
        message=message,
        observed_requested_at=request,
        egg_change_step="",
        egg_change_request=request,
    )


def _empty_recycle_bin(client: PanelClient, identifier: str, logger: kopf.Logger) -> None:
    """The panel keeps wiped files for a day. A missing route means this panel has no bin."""
    try:
        empty_recycle_bin(client, identifier)
    except PanelError as exc:
        if exc.status == 404:
            return
        logger.warning("could not empty the recycle bin: %s", exc.detail)


def _handle_reinstall(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    meta: dict[str, Any],
) -> None:
    """Stop, delete every file, empty the recycle bin, then run the install script again."""
    identifier = status.get("identifier")
    if not identifier:
        set_status(patch, phase=PHASE_ERROR, message="reinstall requires an installed server")
        return
    request = _egg_change_request(spec)
    if _egg_change_finished(status, request):
        return
    client = _client()
    identifier = str(identifier)
    _stop_for_wipe(client, identifier)
    try:
        wipe_server_files(client, identifier)
    except PanelError as exc:
        raise kopf.TemporaryError(exc.detail, delay=30) from exc
    # Deleted files land in the panel trash for a day. Drop them before the
    # installer runs, so a failed install does not leave the old world there.
    _empty_recycle_bin(client, identifier, logger)
    client.client_api("POST", f"/servers/{identifier}/settings/reinstall")
    set_status(patch, phase=PHASE_PENDING, identifier=identifier, message="reinstalling")
    if status.get("serverId"):
        try:
            wait_until_reinstall_finished(client, int(status["serverId"]))
        except InstallFailed:
            _fail_egg_change(patch, str(identifier), request, INSTALL_FAILED_MESSAGE)
            return
        except TimeoutError:
            _fail_egg_change(patch, str(identifier), request, INSTALL_TIMEOUT_MESSAGE)
            return
    _empty_recycle_bin(client, str(identifier), logger)
    _accept_eula(client, spec, str(identifier))
    set_status(
        patch,
        phase=PHASE_READY,
        identifier=str(identifier),
        observed_requested_at=request or None,
    )
    dispatch_gameserver_hook(
        hooks=spec.get("hooks"),
        op_type="reinstalled",
        meta=meta,
        status=status,
        logger=logger,
        name=str(spec.get("name") or ""),
        gameserver_id=_gameserver_id(str(spec.get("externalId") or "")),
        game_ip=str((status.get("allocation") or {}).get("ip") or ""),
        game_port=str((status.get("allocation") or {}).get("port") or ""),
    )


@kopf.on.create(GROUP, VERSION, SERVER_PLURAL)
@kopf.on.update(GROUP, VERSION, SERVER_PLURAL)
def on_server(
    body: dict[str, Any], patch: kopf.Patch, logger: kopf.Logger, meta: dict[str, Any], **_: Any
) -> None:
    _handle(body, patch, logger, meta)


@kopf.on.delete(GROUP, VERSION, SERVER_PLURAL)
def on_delete(body: dict[str, Any], logger: kopf.Logger, meta: dict[str, Any], **_: Any) -> None:
    status = _status(body)
    spec = _spec(body)
    server_id = status.get("serverId")
    if not server_id:
        return
    try:
        row = get_server(_client(), int(server_id))
        if row:
            delete_server(_client(), int(server_id))
    except PanelError as exc:
        if exc.status == 404:
            return
        raise kopf.TemporaryError(exc.detail, delay=15) from exc
    allocation = status.get("allocation") or {}
    dispatch_gameserver_hook(
        hooks=spec.get("hooks"),
        op_type="deleted",
        meta=meta,
        status=status,
        logger=logger,
        name=str(spec.get("name") or ""),
        gameserver_id=_gameserver_id(str(spec.get("externalId") or "")),
        game_ip=str(allocation.get("ip") or ""),
        game_port=str(allocation.get("port") or ""),
    )
