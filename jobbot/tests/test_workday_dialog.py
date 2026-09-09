"""Workday's "Start Your Application" dialog, which offers four ways in."""

from __future__ import annotations

from jobbot import autofill


class _El:
    def __init__(self, text, on_click=None):
        self.text = text
        self._on_click = on_click
        self.clicked = False

    def is_visible(self):
        return True

    def inner_text(self):
        return self.text

    def click(self, timeout=None):
        self.clicked = True
        if self._on_click:
            self._on_click()


class _Ctx:
    """A page showing the dialog; the form appears once the right one is hit."""

    def __init__(self, labels, opens_on):
        self.form_ready = False
        self.frames = []
        self.main_frame = self
        self.els = [
            _El(l, (lambda: setattr(self, "form_ready", True)) if l == opens_on else None)
            for l in labels
        ]

    def query_selector_all(self, selector):
        return self.els if selector == "button" else []

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None


WORKDAY_DIALOG = [
    "Autofill with Resume",
    "Apply Manually",
    "Use My Last Application",
    "Apply With LinkedIn",
]


def _patch(monkeypatch, ctx):
    monkeypatch.setattr(autofill, "form_is_present", lambda c, minimum=3: ctx.form_ready)


def test_apply_manually_is_the_one_clicked(monkeypatch):
    """Autofill with Resume sits above it in the DOM. Taking the first match
    would take that one, which hands Workday the PDF and lets its parser
    overwrite fields — the opposite of this module's whole bargain."""
    ctx = _Ctx(WORKDAY_DIALOG, opens_on="Apply Manually")
    _patch(monkeypatch, ctx)

    assert autofill.open_application_form(ctx, settle_ms=0) == "Apply Manually"
    clicked = [e.text for e in ctx.els if e.clicked]
    assert clicked == ["Apply Manually"]


def test_autofill_with_resume_is_never_clicked(monkeypatch):
    """Even when it is the only option offered."""
    ctx = _Ctx(["Autofill with Resume"], opens_on="Autofill with Resume")
    _patch(monkeypatch, ctx)

    assert autofill.open_application_form(ctx, settle_ms=0) == ""
    assert not any(e.clicked for e in ctx.els)


def test_use_my_last_application_is_never_clicked(monkeypatch):
    """It replays an earlier submission wholesale, which is a different
    application than the one being prepared."""
    ctx = _Ctx(["Use My Last Application"], opens_on="Use My Last Application")
    _patch(monkeypatch, ctx)

    assert autofill.open_application_form(ctx, settle_ms=0) == ""
    assert not any(e.clicked for e in ctx.els)


def test_an_ordinary_apply_button_still_works(monkeypatch):
    """The preferred pass must not break every non-Workday board."""
    ctx = _Ctx(["Apply for this job"], opens_on="Apply for this job")
    _patch(monkeypatch, ctx)

    assert autofill.open_application_form(ctx, settle_ms=0) == "Apply for this job"


def test_submit_is_still_untouchable(monkeypatch):
    ctx = _Ctx(["Submit application"], opens_on="Submit application")
    _patch(monkeypatch, ctx)

    assert autofill.open_application_form(ctx, settle_ms=0) == ""
    assert not any(e.clicked for e in ctx.els)
