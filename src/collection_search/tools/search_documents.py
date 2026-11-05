from typing import Annotated

from pydantic import Field

NAME = "search_documents"
TITLE = "Search my documents"
DESCRIPTION = """
Search the user's own notes and documents (Markdown, Word, PDF and text files
stored on their computer) by meaning and by exact words. Use this whenever the
user asks about "my notes", "my documents", "my files", "my collection" or a
topic they keep notes on. This is for the user's files, not for images, video
or generated media.

Each result is a short passage with its file path, collection, location and,
for Markdown and Word files, the headings above it (for example
"Exodus 20 > 4-6"), so you can often cite it without opening the file.

For a thorough answer, don't stop at one search: search again with other
wording, synonyms and specific names or references; use a limit of 20 or more
for research questions; and ask for the next page with offset when the
results are still relevant. To search only one collection (when the user
names one, like "search my sermon notes"), pass its name as "collection";
list_document_collections shows the names. Then use get_document_links or
explore_documents to follow connections, and read_document with "around" to see a passage in
context.
"""


def handler(
    ctx,
    query: Annotated[str, Field(description="What to look for, in natural language or exact terms")],
    limit: Annotated[int | None, Field(description="How many passages to return (default 15)", ge=1, le=50)] = None,
    offset: Annotated[int, Field(description="Skip this many results, to get the next page", ge=0, le=500)] = 0,
    collection: Annotated[str | None, Field(description="Search only this collection, by name from list_document_collections; leave out to search all")] = None,
) -> dict:
    results, more = ctx.search_page(query, limit, offset, collection)
    out = {"results": results, "count": len(results)}
    if more:
        out["next_offset"] = offset + len(results)
    return out
