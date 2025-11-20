import json
import signal

import pytest
import requests

from opynrefine.client import OpenRefineClient, OpenRefineResponse


class DummyResponse:
    def __init__(self, data=None, *, text="", status_code=200, url="http://example/command"):
        self._data = data
        self.text = text or (json.dumps(data) if data is not None else "")
        self.status_code = status_code
        self.url = url
        self.content = self.text.encode()  # mimic requests API

    def json(self):
        if self._data is None:
            raise ValueError("No JSON available")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class DummySession:
    def __init__(self, response, csrf_response=None):
        self._response = response
        self._csrf_response = csrf_response or DummyResponse({"token": "csrf"})
        self.last_request = None
        self.last_get = None

    def request(self, method, url, **kwargs):
        self.last_request = (method, url, kwargs)
        return self._response

    def get(self, url, **kwargs):
        self.last_get = (url, kwargs)
        return self._csrf_response

    def close(self):
        return None


def test_command_proxy_executes_normalized_path():
    response = DummyResponse({"ok": True})
    session = DummySession(response)
    client = OpenRefineClient("http://example", session=session, auto_register_signals=False)

    result = client.command.core.get_all_project_metadata(params={"foo": "bar"})

    assert result == {"ok": True}
    method, url, kwargs = session.last_request
    assert method == "GET"
    assert url == "http://example/command/core/get-all-project-metadata"
    assert kwargs["params"] == {"foo": "bar"}


def test_signal_interrupt_blocks_requests():
    response = DummyResponse({"ok": True})
    session = DummySession(response)
    client = OpenRefineClient("http://example", session=session, auto_register_signals=False)

    client._handle_signal(signal.SIGINT, None)

    with pytest.raises(RuntimeError):
        client.command.core.get_models()


def test_raw_response_wrapper_returned_when_requested():
    response = DummyResponse(text="plain text")
    session = DummySession(response)
    client = OpenRefineClient("http://example", session=session, auto_register_signals=False)

    result = client.command.core.export_rows(expect_json=False)

    assert isinstance(result, OpenRefineResponse)
    assert result.text == "plain text"


def test_create_project_from_upload_returns_metadata(tmp_path):
    project_file = tmp_path / "test.csv"
    project_file.write_text("foo,bar\n1,2\n", encoding="utf-8")
    response = DummyResponse(text="<html />", url="http://example/project?project=123")
    session = DummySession(response, csrf_response=DummyResponse({"token": "abc"}))
    client = OpenRefineClient("http://example", session=session, auto_register_signals=False)

    result = client.create_project_from_upload(
        "Example Project",
        str(project_file),
        format_hint="text/line-based/*sv",
        format_options={"separator": ","},
    )

    assert result["project_id"] == "123"
    assert session.last_get[0] == "http://example/command/core/get-csrf-token"
    method, url, kwargs = session.last_request
    assert method == "POST"
    assert url == "http://example/command/core/create-project-from-upload"
    assert kwargs["data"]["project-name"] == "Example Project"
    assert kwargs["data"]["format"] == "text/line-based/*sv"
    assert json.loads(kwargs["data"]["options"]) == {"separator": ","}
    assert kwargs["data"]["csrf_token"] == "abc"
    assert kwargs["params"]["csrf_token"] == "abc"
    assert kwargs["files"]["project-file"][0] == "test.csv"


def test_base_url_without_scheme_is_normalized():
    client = OpenRefineClient("127.0.0.1:3333", session=DummySession(DummyResponse({})), auto_register_signals=False)
    assert client.base_url == "http://127.0.0.1:3333"


@pytest.mark.integration
def test_list_projects_against_local_instance():
    client = OpenRefineClient()
    try:
        projects = client.list_projects()
    except requests.exceptions.RequestException as exc:
        pytest.skip(f"OpenRefine not running: {exc}")
    finally:
        client.close()

    assert isinstance(projects, dict)
