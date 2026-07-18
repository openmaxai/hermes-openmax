from hermes_openmax import tools


class FakeTm:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def issue_action(self, issue_id, name, body):
        self.calls.append(("issue", issue_id, name, body))
        return self.result

    async def task_action(self, task_id, name, body):
        self.calls.append(("task", task_id, name, body))
        return self.result


class FakeComm:
    def __init__(self):
        self.sent = []

    async def send_message(self, conversation_id, text, **kwargs):
        self.sent.append((conversation_id, text, kwargs))


class Services:
    def __init__(self, result):
        self.tm = FakeTm(result)
        self.comm = FakeComm()


def test_state_action_notifies_source_conversation_once(monkeypatch):
    services = Services({"id": "task-1", "status": "running", "title": "Build"})
    monkeypatch.setattr(
        tools,
        "_run",
        lambda factory: __import__("asyncio").run(
            factory({"tm": services.tm, "comm": services.comm})
        ),
    )

    args = {"action": "task_action", "task_id": "task-1", "name": "start"}
    first = tools.handle_tasks(args, source_conversation_id="conv-1")
    second = tools.handle_tasks(args, source_conversation_id="conv-1")

    assert first["status"] == "running"
    assert second["status"] == "running"
    assert len(services.comm.sent) == 1
    assert services.comm.sent[0][0] == "conv-1"
    assert "Build" in services.comm.sent[0][1]
    assert services.comm.sent[0][2]["metadata"]["progress_notification"] is True


def test_state_action_without_source_does_not_notify(monkeypatch):
    services = Services({"id": "issue-1", "status": "delivered"})
    monkeypatch.setattr(
        tools,
        "_run",
        lambda factory: __import__("asyncio").run(
            factory({"tm": services.tm, "comm": services.comm})
        ),
    )

    result = tools.handle_tasks(
        {"action": "issue_action", "issue_id": "issue-1", "name": "deliver"}
    )

    assert result["status"] == "delivered"
    assert services.comm.sent == []


def test_read_only_action_does_not_notify(monkeypatch):
    services = Services({"id": "task-1", "status": "running"})
    monkeypatch.setattr(
        tools,
        "_run",
        lambda factory: __import__("asyncio").run(
            factory({"tm": services.tm, "comm": services.comm})
        ),
    )

    tools.handle_tasks(
        {"action": "task_action", "task_id": "task-1", "name": "reassign"},
        source_conversation_id="conv-1",
    )

    assert services.comm.sent == []
