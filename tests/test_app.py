"""The Streamlit dashboard renders headlessly without exceptions."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_renders():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    assert not at.exception, [e.message for e in at.exception]
    assert len(at.tabs) == 6
    assert "PAPER" in at.caption[0].value
