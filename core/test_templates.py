"""Template syntax that fails silently.

Django raises nothing for any of this. The template renders, the page returns
200, and the mistake is visible only to whoever happens to look at the screen —
which is how four leaked comments survived in shipped pages.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings

TEMPLATES = sorted(Path(settings.BASE_DIR / "templates").rglob("*.html"))


def test_there_are_templates_to_check() -> None:
    """Guards the guard: a glob that silently matches nothing would make every
    test below pass for the wrong reason."""
    assert TEMPLATES


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_no_comment_spans_a_newline(path: Path) -> None:
    """`{# #}` is single-line only — Django permits no newline between the
    delimiters. Write one across two lines and it is not a comment at all: the
    text renders to the page verbatim, and nothing anywhere reports it.

    `{% comment %}` is the multi-line form.
    """
    text = path.read_text(encoding="utf-8")
    offenders = [
        match.group(0).splitlines()[0]
        for match in re.finditer(r"\{#(?:(?!#\}).)*\n", text, re.DOTALL)
    ]
    assert not offenders, (
        f"{path.name}: comment opens with {{# and does not close on the same "
        f"line, so it will render to the page. Use {{% comment %}} instead. "
        f"First offender: {offenders[0].strip()!r}"
    )
