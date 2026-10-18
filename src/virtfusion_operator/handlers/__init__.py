"""Kopf handlers for VirtFusion operator resources."""

from __future__ import annotations

import logging
from typing import Any

import kopf

from .. import __version__

from . import control  # noqa: F401
from . import node  # noqa: F401
from . import vm  # noqa: F401
from . import vnc  # noqa: F401
from noderings_hooks import watcher as _hook_watcher  # noqa: F401


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    """Configure Kopf operator settings.

    Do not force peering.standalone here — that would override `kopf run
    --standalone` used for local/lab simulation. In-cluster Helm omits
    --standalone, so Kopf keeps the default (peering on).
    """
    settings.posting.level = logging.INFO
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix="virtfusion-operator.kopf.dev"
    )

    logger = logging.getLogger(__name__)
    logger.info("VirtFusion Operator starting - Version: %s", __version__)
