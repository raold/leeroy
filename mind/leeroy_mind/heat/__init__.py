"""Heat: one dial for how far romance toward the owner goes, in text, voice notes and photos
(README.md). `resolve` is the one pure function everything else reads."""
from .resolve import Member, Resolved, explain, explain_row, own_offset, resolve, what_changes
from .table import PHOTO_TIERS, TIERS

__all__ = ["Member", "Resolved", "explain", "explain_row", "own_offset", "resolve", "what_changes", "TIERS",
           "PHOTO_TIERS"]
