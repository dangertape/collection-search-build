from typing import Annotated

from pydantic import Field

NAME = "get_document_links"
TITLE = "Show a document's links"
DESCRIPTION = """
Show how one of the user's documents connects to others: the notes it links
to ([[wikilinks]], Markdown links and links in web pages) and the notes that
link back to it (backlinks). Pass "collection", "folder" or "file_name" to
list only linked documents in that collection, in that folder and its
subfolders, or whose file names match. Use this after finding a relevant note, before
answering, to see what else the user connected to it. Then read_document with
"around" set to an example's location shows the linking passage.
"""


def handler(
    ctx,
    path: Annotated[str, Field(description="File path exactly as returned by another tool")],
    limit: Annotated[int, Field(description="Most linked documents to list in each direction", ge=1, le=100)] = 30,
    collection: Annotated[str | None, Field(description="Only list linked documents in this collection, by name from list_document_collections")] = None,
    folder: Annotated[str | None, Field(description="Only look in this folder and its subfolders: a full path from another tool, or a path relative to a collection's top folder")] = None,
    file_name: Annotated[str | None, Field(description="Only look in files whose names match: part of a name, or a pattern with * and ? wildcards; separate several with commas")] = None,
) -> dict:
    return ctx.document_links(path, limit, collection, folder, file_name)
