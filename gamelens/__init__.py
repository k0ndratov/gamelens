"""GameLens - a rolling subtitle buffer for games, with on-demand explanations.

Watches the subtitle area of the screen, OCRs only the frames that actually
changed, keeps the last few minutes of dialogue in memory, and answers
questions about it on a hotkey.
"""

__version__ = "0.1.0"
