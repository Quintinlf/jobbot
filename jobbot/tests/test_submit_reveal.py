"""Tests for getting to the submit button.

A filled form is useless if you cannot reach the bottom of it. Two causes look
identical from the user's side — the page simply stops before the button:

  * a consent modal setting overflow:hidden, which locks the whole page
  * the form living in an iframe, which scrolls separately from the page

The hard constraint: none of this may ever press the button. That is the one
guarantee the whole tool rests on, so it is asserted here directly.

Run:  python -m pytest jobbot/tests/ -q
"""

from __future__ import annotations

import inspect
import re

import pytest

from jobbot import autofill


# ── The guarantee ──────────────────────────────────────────────────────────────

def test_nothing_in_the_submit_path_clicks():
    """These locate and scroll to the button. None of them presses it."""
    for fn in (autofill.reveal_submit, autofill.find_submit,
               autofill.release_scroll_lock, autofill.scroll_note,
               autofill.close_open_dropdowns):
        source = inspect.getsource(fn)
        assert not re.search(r"\.click\s*\(", source), f"{fn.__name__} clicks"


def test_the_only_key_pressed_is_escape():
    """Closing a stray menu must not be able to choose from one."""
    for fn in (autofill.reveal_submit, autofill.find_submit,
               autofill.release_scroll_lock, autofill.scroll_note,
               autofill.close_open_dropdowns):
        for match in re.finditer(r"\.press\(([^)]*)\)", inspect.getsource(fn)):
            assert match.group(1) == '"Escape"', f"{fn.__name__}: {match.group(0)}"


def test_only_expanded_dropdowns_are_closed():
    """Escape aimed at a focused text field would discard typed input."""
    source = inspect.getsource(autofill.close_open_dropdowns)
    assert "aria-expanded=true" in source
    assert "combobox" in source


def test_module_still_has_no_submit_call():
    """The original guarantee, re-checked now that submit handling exists."""
    source = inspect.getsource(autofill)
    # Buttons are located and scrolled to; the only clicks are dropdown options
    # and the control that reveals a form hidden behind an Apply tab.
    for match in re.finditer(r"(\w+)\.click\(", source):
        assert match.group(1) in {"chosen", "target", "button", "el"}, match.group(0)


def test_only_two_passes_may_tick_a_checkbox():
    """Ticking is allowed now, but only where the decision came from the profile.

    The blanket "this module never calls .check()" rule was replaced rather
    than dropped: consent has to be fillable for forms that will not submit
    without it, but it must not become something any pass can do in passing.
    """
    allowed = {"_fill_checkbox_groups", "_fill_agreements", "_fill_radio_groups"}
    for name in dir(autofill):
        fn = getattr(autofill, name)
        if not callable(fn) or not hasattr(fn, "__code__"):
            continue
        if getattr(fn, "__module__", "") != autofill.__name__:
            continue
        try:
            source = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if ".check(" in source:
            assert name in allowed, f"{name} ticks a checkbox"


class _RevealAnchor:
    """A plain <a> tag with no role and no "button" class — Lever's own
    control. Clicking it "navigates": the page starts reporting a form."""

    def __init__(self, page, text="Apply for this job", visible=True):
        self._page = page
        self._text = text
        self._visible = visible

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text

    def click(self, timeout=None):
        self._page.form_appears()


class _VisibleField:
    def is_visible(self):
        return True


class _RevealPage:
    def __init__(self, anchors=()):
        self._anchors = list(anchors)
        self._form_visible = False
        self.frames = []
        self.main_frame = self
        self.url = "https://jobs.lever.co/acme/xyz"

    def form_appears(self):
        self._form_visible = True

    def query_selector_all(self, selector):
        if "input[type=text]" in selector:
            return [_VisibleField() for _ in range(3 if self._form_visible else 0)]
        if selector == "a":
            return self._anchors
        return []

    def wait_for_load_state(self, *a, **kw):
        pass

    def wait_for_timeout(self, ms):
        pass


def test_a_bare_anchor_tag_reveals_the_form():
    """Lever's "Apply for this job" is <a class="postings-btn ...">, no
    role=button and no "button" class. The old selector list — a[role=button],
    a.button — never matched it, so the click silently did nothing and the
    form stayed hidden with the run reporting every field unmatched.
    """
    page = _RevealPage(anchors=[_RevealAnchor(None)])
    page._anchors[0]._page = page  # wire the anchor to this page's form_appears

    label = autofill.open_application_form(page)

    assert label == "Apply for this job"
    assert autofill.form_is_present(page) is True


def test_a_submit_labelled_anchor_is_never_clicked_to_reveal_anything():
    """The unconditional guarantee from `_SUBMIT_TEXT` still holds for "a" tags."""
    page = _RevealPage(anchors=[_RevealAnchor(None, text="Submit Application")])
    page._anchors[0]._page = page

    label = autofill.open_application_form(page)

    assert label == ""
    assert autofill.form_is_present(page) is False


def test_an_already_visible_form_is_not_clicked_through_again():
    page = _RevealPage()
    page._form_visible = True
    anchor = _RevealAnchor(page)
    page._anchors = [anchor]
    clicked = []
    anchor.click = lambda timeout=None: clicked.append(True)

    label = autofill.open_application_form(page)

    assert label == ""
    assert clicked == []


def test_revealing_a_form_can_never_click_submit():
    """The Apply-tab opener clicks things. It must not click that thing."""
    source = inspect.getsource(autofill.open_application_form)
    assert "_SUBMIT_TEXT" in source, "must exclude submit-labelled controls"
    assert "_REVEAL_EXCLUDE" in source, "must exclude off-posting links"

    for label in ("Submit", "Submit Application", "Send application"):
        assert not autofill._REVEAL_TEXT.match(label), label


# ── Fakes ──────────────────────────────────────────────────────────────────────

class _El:
    def __init__(self, text="Submit application", visible=True, value=None):
        self._text = text
        self._visible = visible
        self._value = value
        self.scrolled = False

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._value if name == "value" else None

    def scroll_into_view_if_needed(self, timeout=None):
        self.scrolled = True


class _Dropdown:
    def __init__(self):
        self.pressed = []

    def press(self, key):
        self.pressed.append(key)


class _Ctx:
    """Stands in for a page or a frame."""

    def __init__(self, by_selector=None, buttons=(), lock="", controls=0, open_menus=()):
        self._by_selector = by_selector or {}
        self._buttons = list(buttons)
        self._lock = lock
        self._controls = controls
        self._open_menus = list(open_menus)
        self.frames = []
        self.main_frame = self

    def query_selector(self, selector):
        return self._by_selector.get(selector)

    def query_selector_all(self, selector):
        if selector == "[role=combobox][aria-expanded=true]":
            return self._open_menus
        if selector in ("button, [role=button]",):
            return self._buttons
        if "input, textarea, select" in selector:
            return [object()] * self._controls
        return self._by_selector.get(selector, []) if isinstance(
            self._by_selector.get(selector), list) else []

    def evaluate(self, script):
        return self._lock

    def wait_for_timeout(self, ms):
        pass


# ── Finding the button ─────────────────────────────────────────────────────────

def test_finds_greenhouse_submit_by_id():
    el = _El()
    ctx = _Ctx(by_selector={"button#submit_app": el})
    assert autofill.find_submit(ctx) is el


def test_finds_a_plain_button_by_its_text():
    el = _El("Submit application")
    ctx = _Ctx(buttons=[_El("Back"), el])
    assert autofill.find_submit(ctx) is el


def test_ignores_buttons_that_merely_mention_submitting():
    """Full-match only: "Submit a referral instead" is not the submit button."""
    ctx = _Ctx(buttons=[_El("Submit a referral instead"), _El("Save draft")])
    assert autofill.find_submit(ctx) is None


def test_invisible_buttons_are_skipped():
    ctx = _Ctx(by_selector={"button[type=submit]": _El(visible=False)})
    assert autofill.find_submit(ctx) is None


# ── Scrolling to it ────────────────────────────────────────────────────────────

def test_reveal_scrolls_the_button_into_view(monkeypatch):
    el = _El()
    page = _Ctx(by_selector={"button#submit_app": el})
    monkeypatch.setattr(autofill, "form_context", lambda p, **kw: p)

    label, note = autofill.reveal_submit(page)

    assert el.scrolled is True
    assert label == "Submit application"
    assert note is None


def test_reveal_looks_in_the_embedded_frame(monkeypatch):
    """Branded careers pages put the form — and its button — in an iframe."""
    el = _El()
    frame = _Ctx(by_selector={"button#submit_app": el})
    page = _Ctx()
    monkeypatch.setattr(autofill, "form_context", lambda p, **kw: frame)

    label, _ = autofill.reveal_submit(page)

    assert el.scrolled is True
    assert label == "Submit application"


def test_reveal_falls_back_to_the_outer_page(monkeypatch):
    """Form in a frame, submit button rendered by the host page around it."""
    el = _El()
    frame = _Ctx()
    page = _Ctx(by_selector={"button#submit_app": el})
    monkeypatch.setattr(autofill, "form_context", lambda p, **kw: frame)

    label, _ = autofill.reveal_submit(page)
    assert label == "Submit application"


def test_open_menus_are_closed_before_scrolling(monkeypatch):
    """An open react-select menu eats the wheel wherever it sits."""
    menu = _Dropdown()
    el = _El()
    page = _Ctx(by_selector={"button#submit_app": el}, open_menus=[menu])
    monkeypatch.setattr(autofill, "form_context", lambda p, **kw: p)

    autofill.reveal_submit(page)

    assert menu.pressed == ["Escape"]
    assert el.scrolled is True


def test_closing_menus_counts_them():
    menus = [_Dropdown(), _Dropdown()]
    assert autofill.close_open_dropdowns(_Ctx(open_menus=menus)) == 2


def test_no_open_menus_is_a_no_op():
    assert autofill.close_open_dropdowns(_Ctx()) == 0


def test_no_button_reports_rather_than_raising(monkeypatch):
    monkeypatch.setattr(autofill, "form_context", lambda p, **kw: p)
    label, note = autofill.reveal_submit(_Ctx())
    assert label is None


# ── Diagnosing a locked page ───────────────────────────────────────────────────

@pytest.mark.parametrize("lock,expected", [
    ("overflow", "overflow: hidden"),
    ("fixed-body", "position: fixed"),
])
def test_scroll_lock_is_explained(lock, expected):
    note = autofill.scroll_note(_Ctx(lock=lock))
    assert note and expected in note


def test_viewport_taller_than_window_is_explained():
    """The failure that produced no reason at all: nothing is locked, the
    bottom of the page is simply rendered below the window."""
    note = autofill.scroll_note(_Ctx(lock="viewport:950x890"))
    assert note
    assert "950px-tall viewport" in note and "890px window" in note
    assert "launch setting" in note


def test_apply_does_not_pin_the_browser_viewport():
    """A fixed viewport emulates a rectangle unrelated to the real window.

    When it is taller than the window, the page scrolls to the bottom of an
    area the user cannot fully see and the submit button is stranded below the
    screen — with no lock to detect and nothing to report.
    """
    from jobbot import __main__ as cli

    # The launch lives in `_apply_rows`, which is `apply`'s loop lifted out so
    # `hunt` and `prepare-applications` could share the batch flow next to it.
    # Check both, so moving the loop again cannot quietly drop the guarantee.
    source = inspect.getsource(cli._apply_rows) + inspect.getsource(cli.cmd_apply)
    assert "no_viewport=True" in source
    assert not re.search(r"viewport\s*=\s*\{", source), "fixed viewport is back"


def test_unlocked_page_has_no_note():
    assert autofill.scroll_note(_Ctx(lock="")) is None


def test_release_reports_whether_it_changed_anything():
    assert autofill.release_scroll_lock(_Ctx(lock=True)) is True
    assert autofill.release_scroll_lock(_Ctx(lock=False)) is False


def test_release_does_not_dismiss_the_banner():
    """Restoring scroll is not the same as answering a consent dialog."""
    source = inspect.getsource(autofill.release_scroll_lock)
    assert "remove()" not in source
    assert "display" not in source
    assert re.search(r"overflow", source)


# ── Reporting ──────────────────────────────────────────────────────────────────

def test_report_names_the_button_it_scrolled_to():
    report = autofill.FillReport(submit_label="Submit application")
    assert 'Scrolled to the "Submit application" button' in report.render()


def test_report_explains_an_embedded_form_when_no_button_found():
    report = autofill.FillReport(in_frame=True)
    rendered = report.render()
    assert "embedded frame" in rendered
    assert "click inside the form first" in rendered


def test_report_offers_the_unlock_key_when_scrolling_is_blocked():
    report = autofill.FillReport(scroll_note="a consent modal is open")
    assert "[u]" in report.render()
