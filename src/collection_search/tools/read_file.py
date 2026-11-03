from typing import Annotated

from pydantic import Field

NAME = "read_file"
DESCRIPTION = """
Read the full text of one document, given its path from a search result.
Only works for files in a collection that is switched on. Long files are cut
off and marked as truncated.
"""


def handler(
    ctx,
    path: Annotated[str, Field(description="File path exactly as returned by search")],
) -> dict:
    return ctx.read_file(path)
