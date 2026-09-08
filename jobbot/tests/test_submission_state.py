"""Did the application actually go through?

No browser here — these drive `submission_state` with fake page objects, which
is enough because the function only ever asks a page three things: its url, its
visible text, and whether a submit control is still on it.
"""

from __future__ import annotations

from jobbot import autofill


class FakeElement:
    def __init__(self, text="", visible=True):
        self._text, self._visible = text, visible

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text


class FakePage:
    """Minimal stand-in for a Playwright page with no frames."""

    def __init__(self, url="https://boards.greenhouse.io/acme/jobs/1",
                 text="", submit=True, errors=None):
        self.url = url
        self._text = text
        self._submit = submit
        self._errors = errors or []
        self.frames = []
        self.main_frame = None

    def inner_text(self, _selector):
        return self._text

    def query_selector(self, selector):
        if self._submit and selector in autofill._SUBMIT_SELECTORS:
            return FakeElement("Submit Application")
        return None

    def query_selector_all(self, selector):
        if selector in ("button, [role=button]",):
            return [FakeElement("Submit Application")] if self._submit else []
        if selector in ("[aria-invalid=true]", "[role=alert]"):
            return [FakeElement(e) for e in self._errors]
        return []

    def is_closed(self):
        return False

    def wait_for_timeout(self, _ms):
        return None


def test_a_confirmation_url_is_enough():
    page = FakePage(url="https://boards.greenhouse.io/acme/jobs/1/confirmation")
    state, evidence = autofill.submission_state(page)
    assert state == autofill.CONFIRMED
    assert "url" in evidence


def test_lever_thanks_page_counts():
    page = FakePage(url="https://jobs.lever.co/acme/abc123/thanks", submit=False)
    assert autofill.submission_state(page)[0] == autofill.CONFIRMED


def test_confirmation_text_with_the_form_gone():
    page = FakePage(
        text="Your application has been submitted. We will be in touch.",
        submit=False,
    )
    state, evidence = autofill.submission_state(page)
    assert state == autofill.CONFIRMED
    assert "submitted" in evidence


def test_confirmation_wording_above_a_live_form_is_not_a_receipt():
    """Postings that open with "Thanks for applying!" must not read as sent."""
    page = FakePage(
        text="Thanks for applying to Acme! Fill out the form below to apply.",
        submit=True,
    )
    assert autofill.submission_state(page)[0] == autofill.FORM_OPEN


def test_validation_errors_mean_it_did_not_go_through():
    page = FakePage(
        text="Please correct the errors below.",
        submit=True,
        errors=["This field is required", "Please enter a valid phone number"],
    )
    state, evidence = autofill.submission_state(page)
    assert state == autofill.ERRORS
    assert "required" in evidence


def test_an_untouched_form_is_not_submitted():
    page = FakePage(text="Apply for this job. First name. Last name.", submit=True)
    state, evidence = autofill.submission_state(page)
    assert state == autofill.FORM_OPEN
    assert "Submit" in evidence or "form" in evidence


def test_an_unreadable_page_is_unknown_not_a_failure():
    """A board we cannot parse must not be reported as a failed submission."""
    page = FakePage(text="", submit=False)
    assert autofill.submission_state(page)[0] == autofill.UNKNOWN


def test_wait_returns_as_soon_as_it_confirms():
    page = FakePage(url="https://acme.com/apply/thank-you", submit=False)
    state, _ = autofill.wait_for_submission(page, timeout_ms=5000, poll_ms=100)
    assert state == autofill.CONFIRMED


def test_wait_gives_up_without_claiming_success():
    page = FakePage(text="Apply now", submit=True)
    state, _ = autofill.wait_for_submission(page, timeout_ms=300, poll_ms=100)
    assert state == autofill.FORM_OPEN


def test_a_closed_tab_does_not_raise():
    class Closed(FakePage):
        def is_closed(self):
            return True

    state, evidence = autofill.wait_for_submission(Closed(), timeout_ms=1000)
    assert state == autofill.UNKNOWN
    assert "closed" in evidence


def test_a_page_that_throws_is_survivable():
    """Playwright raises on a navigating page; that must not kill the run."""

    class Angry(FakePage):
        def inner_text(self, _selector):
            raise RuntimeError("Execution context was destroyed")

        def query_selector(self, _selector):
            raise RuntimeError("navigating")

        def query_selector_all(self, _selector):
            raise RuntimeError("navigating")

    state, _ = autofill.submission_state(Angry(url="https://acme.com/jobs/1"))
    assert state == autofill.UNKNOWN


# ── Tab lifecycle ──────────────────────────────────────────────────────────────
# The bug this guards: closing every page of a persistent context exits the
# browser, so the next new_page() fails with TargetClosedError and the whole
# prepare-applications run dies before filling anything.

class FakeContext:
    def __init__(self, n_pages=1):
        self.pages = [FakePage() for _ in range(n_pages)]
        for page in self.pages:
            page._closed = False

    def new_page(self):
        if not self.pages:
            raise RuntimeError(
                "TargetClosedError: Target page, context or browser has been closed"
            )
        page = FakePage()
        page._closed = False
        self.pages.append(page)
        return page


def _wire_close(context):
    for page in context.pages:
        _make_closeable(page, context)


def _make_closeable(page, context):
    def close():
        page._closed = True
        if page in context.pages:
            context.pages.remove(page)

    page.close = close
    page.is_closed = lambda: page._closed


def test_retiring_stale_tabs_keeps_the_browser_alive():
    from jobbot.__main__ import _retire_stale_pages

    context = FakeContext(n_pages=4)
    _wire_close(context)

    spare = _retire_stale_pages(context)

    assert spare is not None, "the last page must survive"
    assert len(context.pages) == 1
    assert not spare.is_closed()


def test_the_surviving_tab_is_reusable_as_the_first_one():
    from jobbot.__main__ import _retire_stale_pages

    context = FakeContext(n_pages=1)
    _wire_close(context)

    spare = _retire_stale_pages(context)
    assert spare is context.pages[0]

    # What the fill loop then does: reuse the spare, then open the rest.
    opened = [spare] + [context.new_page() for _ in range(2)]
    assert len(opened) == 3
    assert len(context.pages) == 3


def test_an_empty_context_reports_no_spare():
    from jobbot.__main__ import _retire_stale_pages

    context = FakeContext(n_pages=0)
    assert _retire_stale_pages(context) is None
