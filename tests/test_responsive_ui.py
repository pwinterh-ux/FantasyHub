"""Lightweight contracts for the responsive presentation layer.

These checks intentionally inspect templates rather than pixel output: routes and
submission field names are business contracts that a CSS-only pass must retain.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_base_keeps_viewport_and_loads_responsive_stylesheet():
    base = read("templates/base.html")
    assert 'name="viewport" content="width=device-width, initial-scale=1.0"' in base
    assert "filename='css/responsive.css'" in base
    assert base.index("filename='css/styles.css'") < base.index("filename='css/responsive.css'")


def test_mobile_navigation_reuses_complete_primary_navigation():
    base = read("templates/base.html")
    assert 'id="navToggle"' in base
    assert 'aria-controls="primaryNav"' in base
    assert 'aria-expanded="false"' in base
    assert 'id="primaryNav"' in base
    for endpoint in (
        "start", "mfl.trades_home", "live.live_index", "tools.index",
        "account", "auth.logout", "auth.login", "auth.register",
    ):
        assert f"url_for('{endpoint}')" in base
    assert "https://discord.gg/PKdA8dmTbS" in base
    assert "event.key === 'Escape'" in base
    assert "event.key === 'Escape' && shell.classList.contains('is-open')" in base
    assert "matchMedia('(min-width: 1100px)')" in base


def test_content_flow_reset_is_responsive_only():
    css = read("static/css/responsive.css")
    responsive_start = css.index("@media (max-width: 1199px)")
    reset = "main.content { display: block; min-height: 0;"
    assert reset not in css[:responsive_start]
    assert reset in css[responsive_start:]


def test_tables_keep_native_display_and_wide_tables_use_wrappers():
    css = read("static/css/responsive.css")
    assert "main.content table { display: block" not in css
    assert ".responsive-table" in css
    assert ".league-table-wrap" in css
    assert ".offers-table-wrap" in css
    offers = read("templates/offers/confirm.html")
    assert '<div class="offers-table-wrap rd-scroll-x">' in offers
    assert '<table class="conf-table" id="offers-table">' in offers


def test_rapid_editor_submission_contract_is_unchanged():
    editor = read("templates/lineups/_compact_lineup_editor.html")
    assert 'id="lineupForm" method="POST" action="{{ submit_url }}"' in editor
    assert 'name="league_id"' in editor
    assert 'name="week"' in editor
    assert editor.count('name="starters[]"') == 4
    assert 'id="skipBtn"' in editor
    assert 'type="submit" form="lineupForm"' in editor
    assert 'href="{{ exit_url }}"' in editor


def test_checker_review_keeps_post_and_starter_fields():
    review = read("templates/lineups/checker_review.html")
    assert '{% include "lineups/_compact_lineup_editor.html" %}' in review
    editor = read("templates/lineups/_compact_lineup_editor.html")
    assert 'method="POST"' in editor
    assert 'name="starters[]"' in editor


def test_responsive_breakpoints_and_safe_area_are_present():
    css = read("static/css/responsive.css")
    for breakpoint in ("max-width: 639px", "max-width: 899px", "max-width: 1199px"):
        assert breakpoint in css
    assert "orientation: landscape" in css
    assert "max-height: 520px" in css
    assert "env(safe-area-inset-bottom)" in css
    assert "overflow-x: hidden" not in css


def test_no_literal_duplicate_ids_in_base_template():
    ids = re.findall(r'\bid="([^"]+)"', read("templates/base.html"))
    assert len(ids) == len(set(ids))
