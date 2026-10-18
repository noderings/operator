"""Default SolusVM project for a user."""

from __future__ import annotations

import logging

from .client import SolusVMClient
from .errors import SolusVMHTTPError

logger = logging.getLogger(__name__)


def _owner_id(project: dict) -> int | None:
    owner = project.get("owner") or project.get("user")
    if isinstance(owner, dict) and owner.get("id") is not None:
        return int(owner["id"])
    if project.get("user_id") is not None:
        return int(project["user_id"])
    if project.get("owner_id") is not None:
        return int(project["owner_id"])
    return None


def _projects_for_user(client: SolusVMClient, user_id: int) -> list[dict]:
    """List projects owned by ``user_id``.

    Some SolusVM 2 builds reject ``filter[user_id]`` on GET /projects and only
    allow ``filter[search]``. Fall back to an unfiltered list + owner match.
    """
    try:
        projects = client.list_projects({"filter[user_id]": user_id})
        if projects:
            return projects
    except SolusVMHTTPError as e:
        if e.status_code != 400:
            raise
        logger.warning(
            "GET /projects filter[user_id] not allowed (%s); matching by owner",
            e,
        )
    return [p for p in client.list_projects() if _owner_id(p) == user_id]


def _pick_project_id(projects: list[dict], user_id: int) -> int:
    for project in projects:
        if project.get("is_default") or project.get("isDefault"):
            pid = int(project["id"])
            logger.info("Using default project %s for user %s", pid, user_id)
            return pid
    pid = int(projects[0]["id"])
    logger.info("Using project %s for user %s", pid, user_id)
    return pid


def resolve_project(client: SolusVMClient, user_id: int) -> tuple[int, bool]:
    """Return ``(project_id, send_user_on_create)``.

    ``POST /servers`` rejects ``user`` unless that user owns the project.
    ``POST /users`` on some SolusVM 2 builds does not create a per-user
    project, so we fall back to the management-node default project and omit
    ``user`` (WHMCS omits project and lets the token owner own the server).
    """
    owned = _projects_for_user(client, user_id)
    if owned:
        return _pick_project_id(owned, user_id), True
    projects = list(client.list_projects())
    if not projects:
        raise RuntimeError(f"No SolusVM project found for user {user_id}")
    logger.warning(
        "No SolusVM project owned by user %s; using management-node default project without user",
        user_id,
    )
    return _pick_project_id(projects, user_id), False


def default_project_id(client: SolusVMClient, user_id: int) -> int:
    """Resolve the default project id for a SolusVM user."""
    project_id, _ = resolve_project(client, user_id)
    return project_id
