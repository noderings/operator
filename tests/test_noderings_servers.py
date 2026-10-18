"""The panel client may read and change only servers whose external id starts with NR-."""

import json
from typing import Any, cast

from pterodactyl_operator.client import FOREIGN_SERVER, PanelClient, PanelError, is_noderings_server
from pterodactyl_operator.servers import create_server, noderings_external_id


class _Response:
    def __init__(self, status: int, payload: dict | None) -> None:
        self.status_code = status
        self.reason = "error" if status >= 400 else "OK"
        if payload is None:
            self.content = b""
            self.headers = {"Content-Type": "application/json"}
        else:
            self.content = json.dumps(payload).encode()
            self.headers = {"Content-Type": "application/json"}

    def json(self) -> dict:
        return json.loads(self.content)


class _Session:
    def __init__(self, routes: dict[tuple[str, str], _Response]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[bytes | None] = []

    def request(self, method, url, headers=None, params=None, data=None, timeout=None):
        path = url.split("://", 1)[-1]
        path = "/" + path.split("/", 1)[1]
        self.calls.append((method.upper(), path))
        self.bodies.append(data)
        response = self.routes.get((method.upper(), path))
        if response is None:
            raise AssertionError(f"unexpected {method} {path}")
        return response


def _client(routes: dict[tuple[str, str], _Response]) -> tuple[PanelClient, _Session]:
    client = PanelClient("https://panel.example", "app", "client")
    session = _Session(routes)
    client.session = cast(Any, session)
    return client, session


def _server(name: str, external_id: str = "") -> _Response:
    return _Response(200, {"attributes": {"name": name, "external_id": external_id, "id": 1}})


def test_external_id_is_nr_plus_game_server_id() -> None:
    assert (
        noderings_external_id("0b0dd68c-d1c6-4d38-ab25-199099e8ca51")
        == "NR-0b0dd68c-d1c6-4d38-ab25-199099e8ca51"
    )
    assert noderings_external_id("NR-already") == "NR-already"
    try:
        noderings_external_id("  ")
    except RuntimeError:
        return
    raise AssertionError("blank external id was accepted")


def test_prefix_is_nr_only() -> None:
    assert is_noderings_server("NR-abc")
    assert is_noderings_server(" nr-abc ")
    assert not is_noderings_server("RL-CRAFT")
    assert not is_noderings_server("NRcraft")
    assert not is_noderings_server("")


def test_get_foreign_server_is_refused() -> None:
    client, session = _client({("GET", "/api/application/servers/9"): _server("RL-CRAFT", "26824")})
    try:
        client.application("GET", "/servers/9")
    except PanelError as exc:
        assert exc.status == 403
        assert exc.detail == FOREIGN_SERVER
        assert "RL-CRAFT" not in str(exc)
    else:
        raise AssertionError("foreign server was returned")
    assert session.calls == [("GET", "/api/application/servers/9")]


def test_get_noderings_server_is_allowed() -> None:
    client, _session = _client(
        {("GET", "/api/application/servers/9"): _server("Factorio", "NR-order")}
    )
    _status, payload = client.application("GET", "/servers/9")
    assert payload["attributes"]["name"] == "Factorio"
    assert payload["attributes"]["external_id"] == "NR-order"


def test_power_does_not_reach_a_foreign_server() -> None:
    client, session = _client(
        {("GET", "/api/client/servers/abc"): _server("Someone Else", "not-nr")}
    )
    try:
        client.client_api("POST", "/servers/abc/power", json_body={"signal": "start"})
    except PanelError as exc:
        assert exc.status == 403
    else:
        raise AssertionError("power was sent")
    assert session.calls == [("GET", "/api/client/servers/abc")]


def test_client_call_reads_nr_external_id_from_the_application_api() -> None:
    client, session = _client(
        {
            ("GET", "/api/client/servers/abc"): _Response(
                200,
                {
                    "attributes": {
                        "identifier": "abc",
                        "internal_id": 9,
                        "name": "NR live Minecraft",
                    }
                },
            ),
            ("GET", "/api/application/servers/9"): _server(
                "NR live Minecraft", "NR-3c1ea914-32f7-4047-9055-5798f878cce7"
            ),
            ("POST", "/api/client/servers/abc/files/write"): _Response(204, None),
        }
    )
    status, _payload = client.client_api(
        "POST", "/servers/abc/files/write", raw_body=b"eula=true\n"
    )
    assert status == 204
    assert session.calls == [
        ("GET", "/api/client/servers/abc"),
        ("GET", "/api/application/servers/9"),
        ("POST", "/api/client/servers/abc/files/write"),
    ]


def test_client_call_refuses_a_panel_server_without_nr_external_id() -> None:
    client, session = _client(
        {
            ("GET", "/api/client/servers/abc"): _Response(
                200,
                {"attributes": {"identifier": "abc", "internal_id": 9, "name": "Divine Journey 2"}},
            ),
            ("GET", "/api/application/servers/9"): _server("Divine Journey 2", "37102_wtpD6WR2"),
        }
    )
    try:
        client.client_api("POST", "/servers/abc/files/write", raw_body=b"eula=true\n")
    except PanelError as exc:
        assert exc.status == 403
        assert exc.detail == FOREIGN_SERVER
    else:
        raise AssertionError("a panel server without NR- was modified")
    assert session.calls == [
        ("GET", "/api/client/servers/abc"),
        ("GET", "/api/application/servers/9"),
    ]


def test_power_reaches_an_nr_server() -> None:
    client, session = _client(
        {
            ("GET", "/api/client/servers/abc"): _server("Factorio", "nr-order"),
            ("POST", "/api/client/servers/abc/power"): _Response(204, None),
        }
    )
    status, _payload = client.client_api(
        "POST", "/servers/abc/power", json_body={"signal": "start"}
    )
    assert status == 204
    assert session.calls[-1] == ("POST", "/api/client/servers/abc/power")


def test_create_requires_nr_external_id() -> None:
    client, session = _client({})
    try:
        client.application("POST", "/servers", json_body={"name": "Factorio", "external_id": "abc"})
    except PanelError as exc:
        assert exc.status == 403
    else:
        raise AssertionError("create was sent")
    assert session.calls == []


def test_create_allows_a_customer_hostname() -> None:
    client, session = _client(
        {
            ("POST", "/api/application/servers"): _Response(
                201, {"attributes": {"name": "Factorio", "external_id": "NR-abc"}}
            )
        }
    )
    status, payload = client.application(
        "POST", "/servers", json_body={"name": "Factorio", "external_id": "NR-abc"}
    )
    assert status == 201
    assert payload["attributes"]["name"] == "Factorio"
    assert session.calls == [("POST", "/api/application/servers")]


def test_create_server_stores_hostname_and_nr_external_id() -> None:
    egg = {
        "attributes": {
            "id": 2,
            "startup": "java",
            "docker_image": "img:1",
            "relationships": {"variables": {"data": []}},
        }
    }
    client, session = _client(
        {
            ("GET", "/api/application/nests/1/eggs/2"): _Response(200, egg),
            ("POST", "/api/application/servers"): _Response(
                201, {"attributes": {"id": 9, "name": "Factorio", "external_id": "NR-order"}}
            ),
        }
    )
    created = create_server(
        client,
        name="Factorio",
        user_id=1,
        nest_id=1,
        egg_id=2,
        allocation_id=10,
        external_id="order",
    )
    raw = session.bodies[-1]
    assert raw is not None
    body = json.loads(raw)
    assert body["name"] == "Factorio"
    assert body["external_id"] == "NR-order"
    assert created["id"] == 9


def test_rename_sets_the_customer_hostname() -> None:
    client, session = _client(
        {
            ("GET", "/api/client/servers/abc"): _server("NR-order", "NR-order"),
            ("POST", "/api/client/servers/abc/settings/rename"): _Response(204, None),
        }
    )
    status, _payload = client.client_api(
        "POST", "/servers/abc/settings/rename", json_body={"name": "RL-CRAFT", "description": ""}
    )
    assert status == 204
    assert session.calls[-1] == ("POST", "/api/client/servers/abc/settings/rename")


def test_list_drops_foreign_servers() -> None:
    client, _session = _client(
        {
            ("GET", "/api/application/servers"): _Response(
                200,
                {
                    "data": [
                        {"attributes": {"name": "RL-CRAFT", "external_id": "26824"}},
                        {"attributes": {"name": "Factorio", "external_id": "NR-one"}},
                        {"attributes": {"name": "NR-old", "external_id": "plain-uuid"}},
                    ]
                },
            )
        }
    )
    _status, payload = client.application("GET", "/servers")
    assert [row["attributes"]["name"] for row in payload["data"]] == ["Factorio"]


def test_list_query_string_still_drops_foreign_servers() -> None:
    client, _session = _client(
        {
            ("GET", "/api/application/servers?per_page=100"): _Response(
                200,
                {
                    "data": [
                        {"attributes": {"name": "Factorio", "external_id": "NR-one"}},
                        {"attributes": {"name": "Other", "external_id": "9"}},
                    ]
                },
            )
        }
    )
    _status, payload = client.application("GET", "/servers?per_page=100")
    assert [row["attributes"]["name"] for row in payload["data"]] == ["Factorio"]


def test_node_calls_are_not_server_gated() -> None:
    client, session = _client(
        {("GET", "/api/application/nodes/1"): _Response(200, {"attributes": {"name": "VA-1"}})}
    )
    _status, payload = client.application("GET", "/nodes/1")
    assert payload["attributes"]["name"] == "VA-1"
    assert session.calls == [("GET", "/api/application/nodes/1")]


def test_external_id_of_a_foreign_server_is_refused() -> None:
    client, _session = _client(
        {("GET", "/api/application/servers/external/order-1"): _server("not-ours", "order-1")}
    )
    try:
        client.application("GET", "/servers/external/order-1")
    except PanelError as exc:
        assert exc.status == 403
        assert "not-ours" not in str(exc)
    else:
        raise AssertionError("foreign external id was returned")
