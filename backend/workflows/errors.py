"""The one distinction every workflow hook route has to make: a failure the user
can act on, versus a defect they cannot.

A hook that raises ``WorkflowUserFacingError`` is reporting something the user chose
or configured: a provider rejection, a model that was retired, a key out of credits,
a region the upstream will not serve. A hook that raises anything else is reporting
a bug. Hiding the first behind "see server logs" is the difference between a user
who fixes it in the settings panel and a user who files a bug.

This is the line named once, so the JSON routes -- regenerate, reroll, rehydrate --
report a render failure the same way the SSE one does instead of each inventing its
own. ``_hook_failures`` in api/routes/workflows.py is what acts on it; image_gen's
streaming ``_terminal`` has always drawn the same line inline.

Dependency-free, and outside ``registry.py`` on purpose: the engine layers that
raise these must not import the plugin registry -- and through it the database --
merely to name their own failure.
"""

from __future__ import annotations


class WorkflowUserFacingError(RuntimeError):
    """A hook failure whose message is meant for the user, already sanitized.

    Raising this is a promise about the message: no credentials, no server paths,
    no internals. image_gen's cloud funnel is the reference implementation -- it
    scrubs the provider's own words and caps them long before they reach here.
    """
