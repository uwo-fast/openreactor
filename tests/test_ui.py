import io
import tomllib
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_web import LOCAL, PASSWORD, Bench, until

from openreactor.ui import SOURCE_URL

PAGES = ["/", "/controls", "/runs", "/devices", "/about"]
HX = {"HX-Request": "true", "Origin": LOCAL}
BROWSER = {"Accept": "text/html,application/xhtml+xml", "Origin": LOCAL}
VENDOR = Path(__file__).parents[1] / "src" / "openreactor" / "static" / "vendor"


@pytest.fixture
def ui(tmp_path: Path):
    bench = Bench(tmp_path, password=False)
    with TestClient(bench.app, base_url=LOCAL) as client:
        yield bench, client


@pytest.fixture
def locked_ui(tmp_path: Path):
    bench = Bench(tmp_path, password=True)
    with TestClient(bench.app, base_url=LOCAL) as client:
        yield bench, client


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in ("src", "href", "hx-get", "hx-post", "action", "data-source") and value:
                self.found.append((tag, name, value))


def links(html: str) -> list[tuple[str, str, str]]:
    parser = Links()
    parser.feed(html)
    return parser.found


# Offline


def test_every_page_loads_nothing_from_another_site(ui):
    """The Pi may have no internet: every script, stylesheet, image, request
    and form stays on this server. The one link out is the source code the
    AGPL requires us to offer."""
    _, client = ui
    for page in PAGES:
        html = client.get(page).text
        found = links(html)
        assert found, page
        for tag, attr, value in found:
            if tag == "a" and attr == "href" and value == SOURCE_URL:
                continue
            assert value.startswith("/") and not value.startswith("//"), (page, tag, attr, value)
            if attr in ("src", "href") and value.startswith("/static/"):
                assert client.get(value).status_code == 200, value


def test_the_vendored_files_are_there_and_named_on_the_about_page(ui):
    _, client = ui
    files = tomllib.loads((VENDOR / "vendor.toml").read_text())["file"]
    for f in files:
        assert (VENDOR / f["path"]).stat().st_size > 0, f["path"]
    about = client.get("/about").text
    for f in files:
        assert f"{f['package']} {f['version']}, {f['licence']} licence" in about


def test_every_page_has_stop_all_and_the_footer(ui):
    _, client = ui
    from importlib.metadata import version

    for page in PAGES:
        html = client.get(page).text
        assert 'hx-post="/ui/stop-all"' in html, page
        assert f"openreactor {version('openreactor')}" in html, page
        assert f'href="{SOURCE_URL}"' in html, page
    assert "GNU Affero General Public License" in client.get("/about").text


# The operator's actions


def test_start_a_run_stop_it_and_export_it(ui):
    bench, client = ui
    r = client.post("/ui/runs", data={"name": "brew", "notes": "batch 4"}, headers=HX)
    assert r.status_code == 200
    assert "Run 1 is recording." in r.text
    assert r.headers["HX-Trigger"] == "changed"
    assert "Run 1 is recording." in client.get("/ui/runs").text

    again = client.post("/ui/runs", data={"name": "again"}, headers=HX)
    assert again.status_code == 409 and "run 1 is already recording" in again.text
    assert client.post("/ui/runs", data={"name": "  "}, headers=HX).status_code == 422

    until(lambda: "air.humidity" in client.get("/ui/channels").text)
    r = client.post("/ui/runs/stop", headers=HX)
    assert "Run 1 stopped." in r.text
    listing = client.get("/ui/runs").text
    assert 'href="/api/v1/runs/1/export"' in listing and "batch 4" in listing
    export = client.get("/api/v1/runs/1/export")
    with zipfile.ZipFile(io.BytesIO(export.content)) as z:
        assert "readings.csv" in z.namelist()


def test_send_a_setpoint(ui):
    bench, client = ui
    assert 'hx-post="/ui/channels/jacket/setpoint"' in client.get("/controls").text
    assert 'max="80.0"' in client.get("/controls").text
    r = client.post("/ui/channels/jacket/setpoint", data={"value": "37.04"}, headers=HX)
    assert r.status_code == 200
    assert "jacket set to 37 °C." in r.text
    assert bench.rlht.setpoints == [370, 0]
    r = client.post("/ui/channels/jacket/setpoint", data={"value": "81"}, headers=HX)
    assert r.status_code == 422 and "above its max_setpoint" in r.text
    assert (
        client.post("/ui/channels/jacket/setpoint", data={"value": "nan"}, headers=HX).status_code
        == 422
    )
    assert (
        client.post("/ui/channels/jacket/setpoint", data={"value": "hot"}, headers=HX).status_code
        == 422
    )
    assert (
        client.post("/ui/channels/nothing/setpoint", data={"value": "1"}, headers=HX).status_code
        == 404
    )


def test_stop_all(ui):
    bench, client = ui
    r = client.post("/ui/stop-all", headers=HX)
    assert r.status_code == 200
    assert "Stop-all sent: jacket, heater." in r.text
    assert "alert-success" in r.text
    assert bench.log == ["safe jacket"]


def test_a_stop_all_that_failed_is_shown_as_a_failure(ui):
    """An output that did not go safe must never read as success."""
    bench, client = ui
    actuator = bench.app.state.service.controller.actuators[0]
    actuator.fail = OSError("i2c bus timeout")
    r = client.post("/ui/stop-all", headers=HX)
    assert r.status_code == 502
    assert "alert-danger" in r.text and "alert-success" not in r.text
    assert "Stop-all FAILED for jacket: error: i2c bus timeout" in r.text


def test_calibrate_an_ezo_device(ui):
    bench, client = ui
    page = client.get("/devices").text
    assert 'hx-post="/ui/ezo/air/calibration"' in page
    assert '<option value="temperature">' in page
    assert "air: not calibrated" in client.get("/ui/ezo/air/calibration").text
    r = client.post(
        "/ui/ezo/air/calibration", data={"point": "temperature", "value": "25"}, headers=HX
    )
    assert r.status_code == 200, r.text
    assert "air: calibrated at temperature" in r.text
    assert "cal temperature 25.0" in bench.port.sent
    r = client.post("/ui/ezo/air/calibration/clear", headers=HX)
    assert "air: not calibrated" in r.text


def test_a_bad_calibration_is_explained(ui):
    bench, client = ui
    r = client.post("/ui/ezo/air/calibration", data={"point": "mid", "value": "7"}, headers=HX)
    assert r.status_code == 422
    assert "no calibration point &#39;mid&#39;" in r.text
    r = client.post(
        "/ui/ezo/air/calibration", data={"point": "temperature", "value": "x"}, headers=HX
    )
    assert r.status_code == 422
    assert not any(s.startswith("cal ") for s in bench.port.sent)


def test_what_the_operator_types_is_escaped(ui):
    _, client = ui
    client.post("/ui/runs", data={"name": "<script>alert(1)</script>"}, headers=HX)
    client.post("/ui/runs/stop", headers=HX)
    listing = client.get("/ui/runs").text
    assert "<script>" not in listing and "&lt;script&gt;" in listing


# Signing in


def test_pages_send_a_stranger_to_sign_in(locked_ui):
    _, client = locked_ui
    for page in PAGES:
        r = client.get(page, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/login"), page
    login = client.get("/login")
    assert login.status_code == 200 and 'action="/login"' in login.text
    # The sign-in page's own styles and scripts load without signing in.
    assert client.get("/static/vendor/bootstrap.min.css").status_code == 200


def test_a_browser_signs_in_and_out_with_the_form(locked_ui):
    _, client = locked_ui
    r = client.post("/login", data={"password": "wrong"}, headers=BROWSER, follow_redirects=False)
    assert r.status_code == 401 and "Wrong password." in r.text
    r = client.post("/login", data={"password": PASSWORD}, headers=BROWSER, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/")
    page = client.get("/")
    assert page.status_code == 200 and 'action="/logout"' in page.text
    r = client.post("/logout", headers=BROWSER, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_actions_need_sign_in_and_a_matching_origin(locked_ui):
    bench, client = locked_ui
    r = client.post("/ui/stop-all", headers=HX)
    assert r.status_code == 401
    assert 'class="alert alert-danger' in r.text  # a notice htmx can show, not JSON
    client.post("/login", data={"password": PASSWORD}, headers=BROWSER)
    r = client.post("/ui/stop-all", headers={"HX-Request": "true"})
    assert r.status_code == 403
    r = client.post("/ui/stop-all", headers={"HX-Request": "true", "Origin": "http://evil.example"})
    assert r.status_code == 403
    assert bench.log == []
    assert client.post("/ui/stop-all", headers=HX).status_code == 200
    assert bench.log == ["safe jacket"]


def test_an_ended_session_takes_the_page_to_sign_in(locked_ui):
    _, client = locked_ui
    client.post("/login", data={"password": PASSWORD}, headers=BROWSER)
    assert client.get("/ui/channels", headers=HX).status_code == 200
    client.post("/logout", headers=BROWSER)
    r = client.get("/ui/channels", headers=HX)
    assert r.status_code == 401
    assert r.headers["HX-Redirect"] == "/login"


def test_signing_out_from_another_site_is_refused(locked_ui):
    _, client = locked_ui
    client.post("/login", data={"password": PASSWORD}, headers=BROWSER)
    r = client.post("/logout", headers={"Origin": "http://127.0.0.1:9999"})
    assert r.status_code == 403
    assert client.get("/", follow_redirects=False).status_code == 200


def test_signing_out_without_a_session_needs_no_origin(locked_ui):
    _, client = locked_ui
    assert client.post("/logout").status_code == 204


def test_signing_out_with_no_password_needs_no_origin(ui):
    _, client = ui
    assert client.post("/logout").status_code == 204


def test_no_page_can_be_framed_by_another_site(ui):
    _, client = ui
    for path in ["/", "/runs", "/api/v1/status", "/static/app.js"]:
        r = client.get(path)
        assert r.headers["x-frame-options"] == "DENY", path
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'", path


def test_the_api_still_answers_json(locked_ui):
    _, client = locked_ui
    r = client.get("/api/v1/status")
    assert r.status_code == 401 and r.json()["detail"].startswith("sign in")
