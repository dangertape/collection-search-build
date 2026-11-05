NAME = "list_document_collections"
TITLE = "List my document collections"
DESCRIPTION = """
List the user's document collections (groups of folders of notes and files on
their computer) that are currently switched on. The other document tools only
see files in these collections. To search or explore just one of them, pass
its name as "collection" to search_documents, explore_documents,
find_similar_documents or get_document_links.
"""


def handler(ctx) -> dict:
    return {"active_collections": ctx.active_collections()}
