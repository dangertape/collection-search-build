from typing import Annotated

from pydantic import Field

NAME = "explore_documents"
TITLE = "Explore connected documents"
DESCRIPTION = """
Map how the user's documents connect around a topic or a file, in one call.
Starts from the best matches for a query (or from one file), then follows
links, backlinks, Bible verse references and similar documents outward for a
step or two. Pass "collection" to stay inside one collection (a starting
file must be in it). Returns each document found, how it was reached, and its
passage most relevant to the query, plus the connections between them. Use it for
research and cross-referencing questions ("what do my notes say about X
across the Bible?"), then read the most relevant documents with read_document.
"""


def handler(
    ctx,
    query: Annotated[str | None, Field(description="Topic to explore; also ranks the passages shown")] = None,
    path: Annotated[str | None, Field(description="Or a file to start from, exactly as returned by another tool")] = None,
    steps: Annotated[int, Field(description="How many hops to follow outward", ge=1, le=3)] = 2,
    max_documents: Annotated[int, Field(description="Stop after this many documents", ge=2, le=40)] = 15,
    collection: Annotated[str | None, Field(description="Only find documents in this collection, by name from list_document_collections")] = None,
) -> dict:
    if not (query and query.strip()) and not path:
        raise ValueError("Give a query or a path to start from.")
    return ctx.explore(query, path, steps, max_documents, collection)
