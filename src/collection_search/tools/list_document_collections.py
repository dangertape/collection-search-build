NAME = "list_document_collections"
TITLE = "List my document collections"
DESCRIPTION = """
List the user's document collections (groups of folders of notes and files on
their computer) that are currently switched on, with each one's top folders.
The other document tools only see files in these collections. To look in just
one of them, pass its name as "collection" to search_documents,
explore_documents, find_similar_documents or get_document_links; to look in
one folder and its subfolders, pass "folder". list_document_folders shows the
folders inside each collection.
"""


def handler(ctx) -> dict:
    return {"collections": ctx.collection_folders()}
