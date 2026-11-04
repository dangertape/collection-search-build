NAME = "list_document_collections"
TITLE = "List my document collections"
DESCRIPTION = """
List the user's document collections (groups of folders of notes and files on
their computer) that are currently switched on. The other document tools only
see files in these collections.
"""


def handler(ctx) -> dict:
    return {"active_collections": ctx.active_collections()}
