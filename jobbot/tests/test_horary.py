"""The classifier's job is to tell a service role from a career-grade one.

The regression these tests exist for: an earlier version scanned the posting
body for words like "intern", "support" and "associate" and labelled ten out
of ten competitive engineering roles as sixth-house, because that boilerplate
appears in almost every tech job ad. Body text is only trusted now for phrases
no career-grade posting uses.
"""

from __future__ import annotations

import pytest

from jobbot import horary


@pytest.mark.parametrize(
    "title",
    [
        "Data Entry Clerk",
        "Laboratory Technician I",
        "Research Assistant, Chemistry",
        "Junior Python Developer",
        "Operations Coordinator",
        "IT Helpdesk Technician",
        "Engineering Intern",
    ],
)
def test_service_titles_are_sixth(title):
    assert horary.classify(title).house == horary.SIXTH


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer II",
        "Senior Machine Learning Engineer",
        "Staff Backend Engineer",
        "Principal Data Scientist",
        "Engineering Manager",
        "Machine Learning Engineer",           # bare title -> career-grade default
        "Software Engineer, Applied AI Research",
    ],
)
def test_career_grade_titles_are_tenth(title):
    assert horary.classify(title).house == horary.TENTH


def test_boilerplate_body_does_not_flip_a_career_role():
    """The exact regression: real postings that were misfiled as sixth."""
    body = (
        "Our team supports the platform. We run a summer internship programme "
        "and work closely with the associate product manager. Support engineers "
        "partner with us daily."
    )
    assert horary.classify("Software Engineer, Control Plane", body).house == horary.TENTH
    assert horary.classify("Backend Engineer, AI Engineering", body).house == horary.TENTH


def test_explicit_entry_phrasing_in_body_does_count():
    body = "No prior experience required; we will train the right candidate."
    assert horary.classify("Laboratory Assistant", body).house == horary.SIXTH


def test_low_stated_minimum_is_not_sixth_house_evidence():
    """Competitive research roles say '1+ years' and still screen for more."""
    body = "1+ years of experience building distributed systems."
    assert horary.classify("Machine Learning Research Engineer", body).house == horary.TENTH


def test_years_requirement_pushes_tenth():
    j = horary.classify("Python Developer", "5+ years of professional experience")
    assert j.house == horary.TENTH
    assert j.years_required == 5


def test_perfects_only_for_sixth():
    """`unclear` must never count as support for the chart."""
    assert horary.classify("Data Entry Clerk").perfects is True
    assert horary.classify("Software Engineer II").perfects is False
    assert horary.Judgement(house=horary.UNCLEAR).perfects is False


def test_degree_mention_is_recorded_not_scored():
    j = horary.classify("Data Analyst", "Bachelor's degree required.")
    assert j.degree_mentioned is True
