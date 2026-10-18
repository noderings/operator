"""Typed models for lifecycle hook scripts and Job status."""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field


class HookConcurrencyError(Exception):
    """Too many active hook Jobs; dispatch should be retried."""


class HookAddresses(TypedDict, total=False):
    ipv4: list[str]
    ipv6: list[str]
    ipv4Gateway: str
    ipv6Gateway: str
    ipv4Prefix: str
    ipv6Prefix: str


class VMHooks(BaseModel):
    """Provider scripts keyed by lifecycle hook. Extra keys stay allowed."""

    model_config = ConfigDict(extra="allow")

    created: str | None = None
    started: str | None = None
    stopped: str | None = None
    deleted: str | None = None
    restarted: str | None = None
    reinstalled: str | None = None


class LastHookStatus(BaseModel):
    operation: str | None = None
    state: str | None = None
    ranAt: str | None = None
    message: str | None = None
    jobName: str | None = Field(default=None)
