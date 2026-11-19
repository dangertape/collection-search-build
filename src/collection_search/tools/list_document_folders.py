from typing import Annotated

from pydantic import Field

NAME = "list_document_folders"
TITLE = "List folders in my collections"
DESCRIPTION = """
Show how the user's switched-on collections are organized: the folders that
hold documents, a few levels deep, each with how many documents are inside it
(including its subfolders) and how many subfolders it has. Use it to learn the
layout of a large collection before searching, or to find the folder a
question is about, then pass that folder as "folder" to search_documents,
explore_documents, find_similar_documents or get_document_links to look only
there. Pass "collection" to list one collection, and "folder" to look inside
one folder; raise "depth" to see further down. Only folders with switched-on
documents are listed.
"""


def handler(
    ctx,
    collection: Annotated[str | None, Field(description="Only this collection, by name from list_document_collections")] = None,
    folder: Annotated[str | None, Field(description="Start from this folder: a full path from another tool, or a path relative to a collection's top folder")] = None,
    depth: Annotated[int, Field(description="How many levels of subfolders to show below the starting folders", ge=0, le=10)] = 2,
) -> dict:
    return ctx.folders(collection, folder, depth)
