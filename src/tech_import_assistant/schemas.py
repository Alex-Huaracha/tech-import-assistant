"""Shared data structures between stages: Source, Document, Chunk, SearchResult."""

import re
from typing import Literal

from pydantic import BaseModel, HttpUrl, field_validator

# "49", "49-50" or "210-" (open-ended: to the end of the document)
ARTICLE_RANGE = re.compile(r"^\d+(-\d*)?$")


class Source(BaseModel):
    """One official document from the catalog in sources.toml."""

    id: str
    title: str
    issuer: str
    url: HttpUrl
    format: Literal["html", "pdf"]
    encoding: str = "utf-8"
    keep_articles: list[str] | None = None  # None = the whole document applies

    @field_validator("keep_articles")
    @classmethod
    def check_article_ranges(cls, ranges: list[str] | None) -> list[str] | None:
        for item in ranges or []:
            if not ARTICLE_RANGE.match(item):
                raise ValueError(
                    f"invalid article range {item!r}; use '49', '49-50' or '210-'"
                )
        return ranges
