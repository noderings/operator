"""Watch hook Jobs and record the result on the parent VM CR."""

from __future__ import annotations

from typing import Any

import kopf
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from .constants import (
    ANN_HOOK,
    ANN_HOOK_REPORTED,
    ANN_VM_GROUP,
    ANN_VM_KIND,
    ANN_VM_NAME,
    ANN_VM_NAMESPACE,
    ANN_VM_PLURAL,
    ANN_VM_VERSION,
    LABEL_RUNNER,
)
from .runner import last_hook_patch


@kopf.on.event("batch", "v1", "jobs", labels={LABEL_RUNNER: "true"})
def on_hook_job_event(
    body: dict[str, Any],
    status: dict[str, Any] | None,
    meta: dict[str, Any],
    namespace: str,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    status = status or {}
    succeeded = int(status.get("succeeded") or 0)
    failed = int(status.get("failed") or 0)
    if succeeded <= 0 and failed <= 0:
        return

    annotations = (meta.get("annotations") or {}) if isinstance(meta, dict) else {}
    body_meta = (body.get("metadata") or {}) if isinstance(body, dict) else {}
    annotations = annotations or (body_meta.get("annotations") or {})
    if annotations.get(ANN_HOOK_REPORTED):
        return

    job_name = str(
        (meta.get("name") if isinstance(meta, dict) else None) or body_meta.get("name") or ""
    )
    vm_name = annotations.get(ANN_VM_NAME) or ""
    vm_ns = annotations.get(ANN_VM_NAMESPACE) or namespace
    group = annotations.get(ANN_VM_GROUP) or ""
    version = annotations.get(ANN_VM_VERSION) or "v1alpha1"
    plural = annotations.get(ANN_VM_PLURAL) or ""
    op_type = annotations.get(ANN_HOOK) or ""
    if not (vm_name and group and plural):
        logger.warning("hook job %s missing parent CR annotations; skipping status patch", job_name)
        return

    state = "Succeeded" if succeeded > 0 else "Failed"
    message = f"hook {op_type} {state.lower()}"
    kind = annotations.get(ANN_VM_KIND) or "VirtualMachine"
    api = k8s_client.CustomObjectsApi()
    try:
        api.patch_namespaced_custom_object_status(
            group=group,
            version=version,
            namespace=vm_ns,
            plural=plural,
            name=vm_name,
            body={"status": last_hook_patch(op_type, state, job_name, message)},
        )
    except ApiException as exc:
        if exc.status == 404:
            logger.info("parent CR %s/%s gone; skipping lastHook patch", vm_ns, vm_name)
            return
        logger.warning("failed to patch lastHook on %s/%s: %s", vm_ns, vm_name, exc)
        return

    try:
        batch = k8s_client.BatchV1Api()
        batch.patch_namespaced_job(
            job_name,
            namespace,
            {"metadata": {"annotations": {ANN_HOOK_REPORTED: "true"}}},
        )
    except ApiException as exc:
        logger.warning("failed to mark hook job %s reported: %s", job_name, exc)

    parent = {
        "apiVersion": f"{group}/{version}",
        "kind": kind,
        "metadata": {"name": vm_name, "namespace": vm_ns},
    }
    try:
        kopf.event(
            parent,
            type="Normal" if state == "Succeeded" else "Warning",
            reason=f"LifecycleHook{state}",
            message=message,
        )
    except Exception as exc:  # noqa: BLE001 — event posting must not fail the watch
        logger.warning("failed to emit hook event for %s/%s: %s", vm_ns, vm_name, exc)
