"""Echolot - conversation recorder that lives in the system tray.

Records your microphone and the other side of the conversation together, plus a
JSON Lines log that says who spoke when, so a transcript can be generated later.
"""

#: Semantic version. Middle digit for a new capability, last one for fixes,
#: documentation and tests, first one for a break in how it is operated.
__version__ = "1.2.0"
#: Counter over every change ever shipped, never reset. Answers "is this
#: yesterday's state?", which the semantic number cannot.
__build__ = 5
#: What the user is shown and what goes into a bug report.
VERSION_LABEL = f"{__version__} ({__build__})"
