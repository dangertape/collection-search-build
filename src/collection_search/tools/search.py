from typing import Annotated

from pydantic import Field

NAME = "search"
DESCRIPTION = """
Search the user's documents by meaning and by exact words. Only documents in
the collections the user has switched on are searched. Returns the best
matching passages with their file path, collection and location in the file.
"""


def handler(
    ctx,
    query: Annotated[str, Field(description="What to look for, in natural language or exact terms")],
    limit: Annotated[int, Field(description="How many passages to return", ge=1, le=25)] = 8,
) -> dict:
    results = ctx.search(query, limit)
    return {"results": results, "count": len(results)}
