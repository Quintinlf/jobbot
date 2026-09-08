"""jobbot — find, filter, and queue job applications.

Pipeline:
    verify   probe company slugs to learn which ATS each uses
    refresh  fetch every verified board, score, classify eligibility, store
    queue    review what's reachable, generate materials, mark applied
    stats    funnel counts and response rate

Nothing in this package submits an application. It gets everything ready and
hands you a queue; you press the button.
"""

__version__ = "0.1.0"
