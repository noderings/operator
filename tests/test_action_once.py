from pterodactyl_operator.handlers import action


def setup_function() -> None:
    action.reset_action_claims()


def test_finished_action_is_not_run_again():
    assert action.action_already_finished({"phase": "Ready"})
    assert action.action_already_finished({"phase": "Error"})
    assert not action.action_already_finished({})
    assert not action.action_already_finished(None)
    assert not action.action_already_finished({"phase": "Running"})


def test_second_delivery_does_not_call_the_panel(monkeypatch):
    calls: list[str] = []

    def fake_run(identifier: str, kind: str, payload: dict) -> dict:
        calls.append(kind)
        return {"backup": {"uuid": "one", "name": payload.get("name") or ""}}

    monkeypatch.setattr(action, "_run", fake_run)
    monkeypatch.setattr(action, "_scrub", lambda value: value)
    spec = {"identifier": "abc", "action": "create_backup", "payload": {"name": "nightly"}}

    class Patch:
        def __init__(self) -> None:
            self.status: dict = {}

    first = Patch()
    action.on_action(spec=spec, patch=first, status={}, name="act-1", namespace="ns")
    second = Patch()
    # The follow-up delivery still has an empty status. Liqo does this
    # before the first status write is visible.
    action.on_action(spec=spec, patch=second, status={}, name="act-1", namespace="ns")

    assert calls == ["create_backup"]
    assert first.status["phase"] == "Ready"
    assert second.status == {}
