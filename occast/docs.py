"""Extract documentation from libclang cursors."""

from __future__ import annotations

import re

from clang.cindex import Cursor

from model import DocBlock


def extract_doc(cursor: Cursor) -> DocBlock:
    """Extract documentation from a libclang cursor's comments."""
    brief = cursor.brief_comment or ""
    raw = cursor.raw_comment or ""

    if not raw:
        return DocBlock(brief=brief)

    # Clean the raw comment: strip //! prefix and leading whitespace
    lines = []
    for line in raw.split("\n"):
        # Remove //! or /// prefix
        line = re.sub(r'^\s*///?\s?', '', line)
        lines.append(line)

    cleaned = "\n".join(lines).strip()

    # Parse @param, @return, @note tags
    params = {}
    returns = ""
    notes = []
    current_tag = None
    current_tag_arg = ""
    current_text = []

    for line in cleaned.split("\n"):
        stripped = line.strip()

        # Check for @tag patterns
        tag_match = re.match(r'@(param|return|note|warning|see|brief)\s*(\w*)\s*(.*)', stripped)
        if tag_match:
            # Save previous tag
            if current_tag == "param" and current_tag_arg:
                params[current_tag_arg] = " ".join(current_text).strip()
            elif current_tag == "return":
                returns = " ".join(current_text).strip()
            elif current_tag in ("note", "warning"):
                notes.append(" ".join(current_text).strip())

            current_tag = tag_match.group(1)
            current_tag_arg = tag_match.group(2)
            current_text = [tag_match.group(3)] if tag_match.group(3) else []
        else:
            current_text.append(stripped)

    # Save last tag
    if current_tag == "param" and current_tag_arg:
        params[current_tag_arg] = " ".join(current_text).strip()
    elif current_tag == "return":
        returns = " ".join(current_text).strip()
    elif current_tag in ("note", "warning"):
        notes.append(" ".join(current_text).strip())

    return DocBlock(
        brief=brief,
        raw=cleaned,
        params=params,
        returns=returns,
        notes=[n for n in notes if n],
    )
