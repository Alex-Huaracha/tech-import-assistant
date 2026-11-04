"""Shared data structures between stages: Source, Article, Chunk, SearchResult."""

import re
from typing import Literal

from pydantic import BaseModel, HttpUrl, field_validator

# "49", "49-50" or "210-" (open-ended: to the end of the document)
ARTICLE_RANGE = re.compile(r"^(\d+)(?:-(\d*))?$")


class Source(BaseModel):
    """One official document from the catalog in sources.toml."""

    id: str
    title: str
    issuer: str
    url: HttpUrl
    format: Literal["html", "pdf"]
    # How the text is organized: laws are split into numbered articles,
    # SUNAT procedures into sections and literals (A.1, A.2...).
    structure: Literal["articles", "procedure"]
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

    def keeps(self, number: int) -> bool:
        """Whether article `number` is inside the scope defined by keep_articles."""
        if self.keep_articles is None:
            return True
        for item in self.keep_articles:
            match = ARTICLE_RANGE.match(item)
            assert match  # guaranteed by the validator
            start = int(match.group(1))
            end_text = match.group(2)
            if end_text is None:  # "49"
                end = start
            elif end_text == "":  # "210-"
                end = float("inf")
            else:  # "49-50"
                end = int(end_text)
            if start <= number <= end:
                return True
        return False


class Article(BaseModel):
    """One article of a law or regulation, as extracted from the source."""

    number: int
    suffix: str = ""  # "A" in "Artículo 145-A"
    title: str
    location: list[str]  # e.g. ["SECCIÓN SEXTA ...", "TÍTULO I ...", "CAPÍTULO II ..."]
    paragraphs: list[str]
    informes: list[str]  # SUNAT reports that interpret this article

    @property
    def label(self) -> str:
        return f"{self.number}-{self.suffix}" if self.suffix else str(self.number)
