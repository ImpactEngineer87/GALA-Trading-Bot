from pathlib import Path


def test_todos_option_clears_previous_ticker_filter():
    html = Path("dashboard/index.html").read_text(encoding="utf-8")

    # The Todos option has value="". It must be allowed to remain empty instead
    # of falling back to the previously selected ticker stored in S.
    assert 'onchange="selectSignalLearningTicker(this.value)"' in html
    assert "const ticker=((select ? select.value : S.signalLearningTicker) || '').trim().toUpperCase();" in html
    assert "select?.value || S.signalLearningTicker" not in html


def test_current_version_keeps_todos_fix():
    html = Path("dashboard/index.html").read_text(encoding="utf-8")
    version = Path("VERSION").read_text(encoding="utf-8").strip()
    assert version == "3.3.13"
    assert "Trading Algorítmico v3.3.13" in html
