"""SolusVM project lookup: filter[user_id] 400 falls back to owner match."""

from __future__ import annotations

import unittest

from solusvm_operator.errors import SolusVMHTTPError
from solusvm_operator.projects import default_project_id, resolve_project


class _FakeClient:
    def __init__(self, filtered=None, filtered_error=None, all_projects=None):
        self.filtered = filtered
        self.filtered_error = filtered_error
        self.all_projects = all_projects or []
        self.calls: list[dict | None] = []

    def list_projects(self, params: dict | None = None):
        self.calls.append(params)
        if params and "filter[user_id]" in params:
            if self.filtered_error is not None:
                raise self.filtered_error
            return list(self.filtered or [])
        return list(self.all_projects)


class DefaultProjectTests(unittest.TestCase):
    def test_prefers_is_default(self):
        client = _FakeClient(
            filtered=[
                {"id": 1, "user_id": 7},
                {"id": 2, "user_id": 7, "is_default": True},
            ]
        )
        self.assertEqual(default_project_id(client, 7), 2)

    def test_falls_back_when_user_id_filter_rejected(self):
        client = _FakeClient(
            filtered_error=SolusVMHTTPError(
                "Requested filter(s) `user_id` are not allowed. Allowed filter(s) are `search`.",
                status_code=400,
            ),
            all_projects=[
                {"id": 9, "owner": {"id": 1}},
                {"id": 11, "owner": {"id": 7}, "isDefault": True},
                {"id": 12, "user_id": 7},
            ],
        )
        self.assertEqual(default_project_id(client, 7), 11)
        self.assertEqual(client.calls[0], {"filter[user_id]": 7})
        self.assertIsNone(client.calls[1])

    def test_owner_match_when_filter_returns_empty(self):
        client = _FakeClient(
            filtered=[],
            all_projects=[{"id": 4, "owner_id": 7}],
        )
        self.assertEqual(default_project_id(client, 7), 4)

    def test_raises_when_none_for_user(self):
        client = _FakeClient(filtered=[], all_projects=[])
        with self.assertRaisesRegex(RuntimeError, "user 7"):
            default_project_id(client, 7)

    def test_falls_back_to_mn_default_when_user_has_no_project(self):
        client = _FakeClient(
            filtered=[],
            all_projects=[
                {"id": 3, "owner": {"id": 1}},
                {"id": 1, "owner": {"id": 1}, "is_default": True},
            ],
        )
        self.assertEqual(default_project_id(client, 7), 1)
        pid, send_user = resolve_project(client, 7)
        self.assertEqual(pid, 1)
        self.assertFalse(send_user)

    def test_send_user_when_user_owns_project(self):
        client = _FakeClient(
            filtered=[{"id": 2, "user_id": 7, "is_default": True}],
        )
        pid, send_user = resolve_project(client, 7)
        self.assertEqual(pid, 2)
        self.assertTrue(send_user)


if __name__ == "__main__":
    unittest.main()
