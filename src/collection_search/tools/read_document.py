from typing import Annotated

from pydantic import Field

NAME = "read_document"
TITLE = "Read a document"
DESCRIPTION = """
Read one of the user's documents, given its path from a search result. To see
a passage in context, pass "around" with the result's location (like
"paragraphs 12-15") or heading (like "Exodus 20 > 4"): you get that part plus
a few sections either side, which is quicker than the whole file. Leave
"around" out to read the whole file. Each section starts with its location and
headings in square brackets. Very long files are cut off and marked as
truncated. Only works for files in a collection that is switched on.
"""


def handler(
    ctx,
    path: Annotated[str, Field(description="File path exactly as returned by another tool")],
    around: Annotated[str | None, Field(description="Location or heading to read around; leave out for the whole file")] = None,
    context: Annotated[int, Field(description="Sections to include before and after 'around'", ge=0, le=50)] = 4,
) -> dict:
    return ctx.read_file(path, around, context)
