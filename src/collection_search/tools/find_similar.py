from typing import Annotated

from pydantic import Field

NAME = "find_similar"
DESCRIPTION = """
Find documents similar to one document, like a "related notes" panel. Give the
path of a file from search, read_file or an earlier find_similar result. Only
documents in the collections the user has switched on are compared. Each result
has an overall score, a whole-document similarity, a passage similarity, and
its best matching passages with the part of the source file each one matches.
"""


def handler(
    ctx,
    path: Annotated[str, Field(description="File path exactly as returned by search or read_file")],
    limit: Annotated[int, Field(description="How many similar documents to return", ge=1, le=25)] = 8,
    passages: Annotated[int, Field(description="Matching passages to show per document", ge=0, le=5)] = 2,
) -> dict:
    result = ctx.find_similar(path, limit, passages)
    return result | {"count": len(result["results"])}
