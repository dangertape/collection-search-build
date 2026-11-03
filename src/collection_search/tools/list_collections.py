NAME = "list_collections"
DESCRIPTION = """
List the document collections that are currently switched on. Search and
read_file only see documents in these collections.
"""


def handler(ctx) -> dict:
    return {"active_collections": ctx.active_collections()}
