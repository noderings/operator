"""Kopf must keep its diff-base under the annotation Liqo leaves alone.

Liqo re-applies the platform's copy of each CR on the provider cluster and
drops annotations it does not know. It is configured to keep only
"<operator>-operator.kopf.dev/last-handled-configuration". If kopf stores its
diff-base anywhere else, every CR looks new on every sync and is re-handled.
"""

import kopf
import pytest
from kopf._cogs.structs import bodies, patches


def _configure(module_path):
    module = __import__(module_path, fromlist=["configure"])
    settings = kopf.OperatorSettings()
    module.configure(settings=settings)
    return settings


@pytest.mark.parametrize(
    ("module_path", "prefix"),
    [
        ("proxmox_operator.handlers", "proxmox-operator.kopf.dev"),
        ("virtfusion_operator.handlers", "virtfusion-operator.kopf.dev"),
        ("solusvm_operator.handlers", "solusvm-operator.kopf.dev"),
    ],
)
def test_diffbase_uses_liqo_protected_annotation(module_path, prefix):
    settings = _configure(module_path)
    patch = patches.Patch()
    settings.persistence.diffbase_storage.store(
        body=bodies.Body({"metadata": {"annotations": {}}, "spec": {"a": 1}}),
        patch=patch,
        essence={"spec": {"a": 1}},
    )
    written = patch["metadata"]["annotations"]
    assert f"{prefix}/last-handled-configuration" in written
    assert not any(k.startswith("kopf.zalando.org/") for k in written)
