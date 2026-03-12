import json
import signal
import pytest
import requests
from opynrefine.client import OpenRefineClient

class DummyResponse:
    def __init__(self, data=None, *, text="", status_code=200, url="http://example/command", headers=None):
        self._data = data
        self.text = text or (json.dumps(data) if data is not None else "")
        self.status_code = status_code
        self.url = url
        self.content = self.text.encode()
        self.headers = headers or {}

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
        pass

@pytest.fixture
def mock_client():
    response = DummyResponse({"ok": True})
    session = DummySession(response)
    client = OpenRefineClient("http://example", session=session, auto_register_signals=False)
    return client, session

def test_command_proxy_returns_response(mock_client):
    client, session = mock_client
    # command proxy now returns a Response object directly
    response = client.command.core.get_all_project_metadata(params={"foo": "bar"})
    
    assert isinstance(response, DummyResponse) # In real usage, requests.Response
    assert response.json() == {"ok": True}
    
    method, url, kwargs = session.last_request
    assert method == "GET"
    assert url == "http://example/command/core/get-all-project-metadata"
    assert kwargs["params"] == {"foo": "bar"}

def test_signal_interrupt_blocks_requests(mock_client):
    client, _ = mock_client
    client._handle_signal(signal.SIGINT, None)
    with pytest.raises(RuntimeError):
        client.command.core.get_models()

def test_execute_command_returns_response(mock_client):
    client, _ = mock_client
    # execute_command should return the raw Response object
    response = client.execute_command("core/test")
    assert isinstance(response, DummyResponse) # In real usage, requests.Response

def test_list_projects(mock_client):
    client, session = mock_client
    session._response = DummyResponse({"projects": {}})
    
    result = client.list_projects()
    assert result == {"projects": {}}
    
    method, url, _ = session.last_request
    assert method == "GET"
    assert "get-all-project-metadata" in url

def test_delete_project(mock_client):
    client, session = mock_client
    client.delete_project("123")
    
    method, url, kwargs = session.last_request
    assert method == "POST"
    assert "delete-project" in url
    assert kwargs["data"]["project"] == "123"
    assert "csrf_token" in kwargs["data"]

def test_get_models(mock_client):
    client, session = mock_client
    client.get_models("123")
    
    method, url, kwargs = session.last_request
    assert method == "GET"
    assert "get-models" in url
    assert kwargs["params"]["project"] == "123"

def test_get_rows(mock_client):
    client, session = mock_client
    client.get_rows("123", limit=10)
    
    method, url, kwargs = session.last_request
    assert method == "GET"
    assert "get-rows" in url
    assert kwargs["params"]["project"] == "123"
    assert kwargs["params"]["limit"] == 10

def test_export_rows_returns_response(mock_client):
    client, session = mock_client
    session._response = DummyResponse(text="tsv_data")
    
    # export_rows returns the raw Response object
    response = client.export_rows("123")
    
    assert isinstance(response, DummyResponse)
    assert response.text == "tsv_data"
    
    method, url, kwargs = session.last_request
    assert method == "POST"
    assert "export-rows" in url
    assert kwargs["data"]["project"] == "123"
    assert kwargs["data"]["format"] == "tsv"
    # Verify default engine
    assert kwargs["data"]["engine"] == '{"facets": [], "mode": "row-based"}'


def test_export_rows_raises_on_html_response(mock_client):
    """OpenRefine can return 200 + text/html when a project is corrupt/missing."""
    client, session = mock_client
    html_error = (
        "<html><title>Error 500</title>"
        "<body>Failed to find project id #2569674056980 - may be corrupt</body></html>"
    )
    session._response = DummyResponse(
        text=html_error,
        headers={"Content-Type": "text/html;charset=ISO-8859-1"},
    )

    with pytest.raises(RuntimeError, match="HTML error page"):
        client.export_rows("2569674056980")

def test_apply_operations(mock_client):
    client, session = mock_client
    ops = [{"op": "test"}]
    client.apply_operations("123", ops)
    
    method, _, kwargs = session.last_request
    assert method == "POST"
    assert kwargs["data"]["project"] == "123"
    assert json.loads(kwargs["data"]["operations"]) == ops

def test_compute_facets(mock_client):
    client, session = mock_client
    facets = [{"name": "test"}]
    client.compute_facets("123", facets)
    
    method, _, kwargs = session.last_request
    assert method == "POST"
    assert kwargs["data"]["project"] == "123"
    assert json.loads(kwargs["data"]["engine"]) == facets

def test_create_project_from_url(mock_client):
    client, session = mock_client
    client.create_project_from_url("Test", "http://data.com")
    
    method, _, kwargs = session.last_request
    assert method == "POST"
    assert kwargs["data"]["projectName"] == "Test"
    assert kwargs["data"]["url"] == "http://data.com"

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
    # Verify options are passed correctly
    assert json.loads(kwargs["data"]["options"]) == {"separator": ","}

def test_base_url_without_scheme_is_normalized():
    client = OpenRefineClient("127.0.0.1:3333", session=DummySession(DummyResponse({})), auto_register_signals=False)
    assert client.base_url == "http://127.0.0.1:3333"

@pytest.mark.integration
def test_list_projects_against_local_instance():
    client = OpenRefineClient()
    try:
        projects = client.list_projects()
        # If running, it should return a dict
        assert isinstance(projects, dict)
    except requests.exceptions.RequestException as exc:
        pytest.skip(f"OpenRefine not running: {exc}")
    finally:
        client.close()
