from typing import Annotated

from pydantic import Field

NAME = "find_similar_documents"
TITLE = "Find similar documents"
DESCRIPTION = """
Find the user's documents that are similar to one document, like a "related
notes" panel. Good for cross-references: start from the most relevant file and
see which others cover the same ground, even when they don't link to it. Give
the path of a file from another tool. Only documents in the collections the
user has switched on are compared; pass "collection" to compare only within
that one (the file itself must be in it too). Each result has an overall
score, a whole-document similarity, a passage similarity, and its best matching
passages with the part of the source file each one matches.
"""


def handler(
    ctx,
    path: Annotated[str, Field(description="File path exactly as returned by another tool")],
    limit: Annotated[int, Field(description="How many similar documents to return", ge=1, le=50)] = 10,
    passages: Annotated[int, Field(description="Matching passages to show per document", ge=0, le=5)] = 2,
    collection: Annotated[str | None, Field(description="Only compare with documents in this collection, by name from list_document_collections")] = None,
) -> dict:
    result = ctx.find_similar(path, limit, passages, collection)
    return result | {"count": len(result["results"])}
