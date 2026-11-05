from typing import Annotated

from pydantic import Field

NAME = "get_document_links"
TITLE = "Show a document's links"
DESCRIPTION = """
Show how one of the user's documents connects to others: the notes it links
to ([[wikilinks]] and Markdown links), the notes that link back to it
(backlinks), and the Bible verse references it mentions, such as
"Exodus 20:4". A Bible chapter file (like "Exod-20.md") counts as linked from
every note that mentions a verse in that chapter, so its backlinks show where
the chapter is discussed. Pass "collection" to list only linked documents in
that collection. Use this after finding a relevant note, before
answering, to see what else the user connected to it. Then read_document with
"around" set to an example's location shows the linking passage.
"""


def handler(
    ctx,
    path: Annotated[str, Field(description="File path exactly as returned by another tool")],
    limit: Annotated[int, Field(description="Most linked documents to list in each direction", ge=1, le=100)] = 30,
    collection: Annotated[str | None, Field(description="Only list linked documents in this collection, by name from list_document_collections")] = None,
) -> dict:
    return ctx.document_links(path, limit, collection)
