"""SSH key reuse when the key is owned by admin (create omits user)."""

from __future__ import annotations

import hashlib
import unittest

from solusvm_operator.errors import SolusVMHTTPError
from solusvm_operator.sshkeys import _fingerprint, ensure_ssh_keys

PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEtestmaterial e2e"


class _FakeClient:
    def __init__(self, user_keys=None, all_keys=None, create_error=None, created=None):
        self.user_keys = list(user_keys or [])
        self.all_keys = list(all_keys or [])
        self.create_error = create_error
        self.created = created or {"id": 99}
        self.creates = 0
        self.list_calls: list[int | None] = []

    def list_ssh_keys(self, user_id=None):
        self.list_calls.append(user_id)
        if user_id is None:
            return list(self.all_keys)
        return list(self.user_keys)

    def create_ssh_key(self, name, body, user_id):
        self.creates += 1
        if self.create_error is not None:
            raise self.create_error
        return {"data": dict(self.created)}


class EnsureSSHKeysTests(unittest.TestCase):
    def test_reuses_admin_owned_key_by_fingerprint(self):
        fp = _fingerprint(PUB)
        client = _FakeClient(
            user_keys=[],
            all_keys=[{"id": 7, "name": f"nr-{fp}", "body": PUB, "user_id": 1}],
        )
        ids = ensure_ssh_keys(client, 2, [PUB])
        self.assertEqual(ids, [7])
        self.assertEqual(client.creates, 0)

    def test_reuses_on_422_name_taken(self):
        fp = hashlib.sha256(PUB.strip().encode()).hexdigest()[:16]
        name = f"nr-{fp}"
        client = _FakeClient(
            user_keys=[],
            all_keys=[],
            create_error=SolusVMHTTPError(
                "The name has already been taken.; name: The name has already been taken.",
                status_code=422,
            ),
        )

        def list_ssh_keys(user_id=None):
            client.list_calls.append(user_id)
            if client.creates:
                return [{"id": 12, "name": name, "body": PUB, "user_id": 1}]
            return []

        client.list_ssh_keys = list_ssh_keys  # type: ignore[method-assign]
        ids = ensure_ssh_keys(client, 2, [PUB])
        self.assertEqual(ids, [12])
        self.assertEqual(client.creates, 1)
