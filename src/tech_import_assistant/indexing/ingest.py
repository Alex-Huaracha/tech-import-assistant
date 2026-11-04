"""Turn the raw documents in data/raw/ into clean Markdown in knowledge_base/.

For laws and regulations (structure = "articles"):
    1. decode     with the encoding recorded in sources.toml (not the server's)
    2. blocks     cut the HTML at every block element, so text split by Word
                  into many <span>s is joined back into whole paragraphs
    3. articles   detect "Artículo 49º.-" headings and group the paragraphs
    4. metadata   pull SUNAT report references out of the text
    5. scope      keep only the articles listed in keep_articles
    6. write      knowledge_base/<id>.md, one "## Artículo" per article

Usage:
    uv run python -m tech_import_assistant.indexing.ingest
"""

import json
import re
from collections import Counter

from bs4 import BeautifulSoup

from tech_import_assistant.config import settings
from tech_import_assistant.indexing.download import load_sources, raw_path
from tech_import_assistant.schemas import Article, Source

BLOCK_TAGS = ["p", "div", "td", "th", "tr", "li", "br", "table"]
BLOCK_TAGS += ["h1", "h2", "h3", "h4", "h5", "h6"]
SEPARATOR = "\x1e"  # ASCII record separator: never appears in the documents

# "Artículo 49º.- Título", "Artículo 145-A.- ...", "Artículo 62A°.- ...",
# "Artículo 139º Título" (no ".-"), "Artículo 127. Título Texto..."
ARTICLE_HEADING = re.compile(
    r"^Art[íi]culo\s+(\d+)\s*(?:-?\s*([A-Z])\b)?\s*[º°]?\s*[.\-–]*\s*(.*)$"
)
LOCATION_HEADING = re.compile(r"^(SECCI[ÓO]N|T[ÍI]TULO|CAP[ÍI]TULO)\s+\S+")
END_OF_ARTICLES = re.compile(r"^DISPOSICIONES\s+COMPLEMENTARIAS")
INFORME = re.compile(r"Informe\s*N[°º]\s*(\d+-\d{4}-SUNAT/\d+)")
ONLY_INFORMES = re.compile(r"^(?:\s*Informe\s*N[°º]\s*\d+-\d{4}-SUNAT/\d+\s*)+$")

MAX_TITLE_LENGTH = 120
# A title alone in its own block can be longer: there is no text to split from.
MAX_UNSPLIT_TITLE_LENGTH = 200


# --- 1-2. Decode and split into blocks -------------------------------------


def read_blocks(source: Source) -> list[str]:
    """Return the document's text as a list of paragraphs, in reading order."""
    html = raw_path(source).read_bytes().decode(source.encoding)
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style"]):
        tag.decompose()

    # Mark the boundaries of every block element. Inline elements (<span>,
    # <b>, <a>) get no mark, so their pieces join into one paragraph.
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before(SEPARATOR)
        tag.insert_after(SEPARATOR)

    blocks = (
        re.sub(r"\s+", " ", text).strip() for text in soup.get_text().split(SEPARATOR)
    )
    return [block for block in blocks if block]


# --- 3-4. Group blocks into articles ---------------------------------------


def split_heading(rest: str) -> tuple[str, str]:
    """Split what follows "Artículo N.-" into (title, first paragraph).

    Usually the heading is only the title ("Objeto"), but some articles put
    title and text together ("Transbordo. El transportista...").
    """
    if len(rest) <= MAX_TITLE_LENGTH:
        return rest.rstrip(":. "), ""
    period = rest.find(". ")
    if 0 < period <= MAX_TITLE_LENGTH:
        return rest[:period], rest[period + 2 :]
    # A long title alone: no sentence inside and no final period.
    if (
        period == -1
        and len(rest) <= MAX_UNSPLIT_TITLE_LENGTH
        and not rest.endswith(".")
    ):
        return rest, ""
    return "", rest


def parse_articles(blocks: list[str]) -> list[Article]:
    articles: list[Article] = []
    location: dict[str, str] = {}  # level ("SECCIÓN", "TÍTULO"...) -> heading
    last_key = (0, "")
    pending_location: str | None = None  # level waiting for its name block

    for block in blocks:
        if END_OF_ARTICLES.match(block):
            break

        heading = ARTICLE_HEADING.match(block)
        if heading:
            key = (int(heading.group(1)), heading.group(2) or "")
            # Articles only go forward. A smaller number at the start of a
            # paragraph is a citation of another norm, not a new article.
            if key >= last_key:
                last_key = key
                pending_location = None
                title, first_paragraph = split_heading(heading.group(3))
                articles.append(
                    Article(
                        number=key[0],
                        suffix=key[1],
                        title=title,
                        location=list(location.values()),
                        paragraphs=[first_paragraph] if first_paragraph else [],
                        informes=[],
                    )
                )
                continue

        level = LOCATION_HEADING.match(block)
        if level:
            name = level.group(1).upper().replace("Ó", "O").replace("Í", "I")
            # A new section resets the titles and chapters below it.
            order = ["SECCION", "TITULO", "CAPITULO"]
            for lower in order[order.index(name) :]:
                location.pop(lower, None)
            location[name] = block
            pending_location = name
            continue

        if (
            pending_location
            and len(block) <= MAX_TITLE_LENGTH
            and not INFORME.search(block)
        ):
            # The block right after "CAPÍTULO II" is its name.
            location[pending_location] += f" {block}"
            pending_location = None
            continue
        pending_location = None

        if not articles:
            continue  # preamble before Article 1

        current = articles[-1]
        current.informes += INFORME.findall(block)
        if ONLY_INFORMES.match(block):
            continue
        text = INFORME.sub("", block).strip()

        # Heading without a title ("Artículo 204.-"): the title is the next
        # block, sometimes with the ".-" left over (". Liberación de...").
        if not current.title and not current.paragraphs:
            candidate = text.lstrip(".-– ")
            if len(candidate) <= MAX_UNSPLIT_TITLE_LENGTH and not candidate.endswith(
                "."
            ):
                current.title = candidate
                continue

        current.paragraphs.append(text)

    return articles


# --- 5-6. Scope and write ---------------------------------------------------


def check_articles(source: Source, articles: list[Article]) -> list[str]:
    """Warnings about duplicated or missing articles inside the scope."""
    warnings = []
    counts = Counter(article.label for article in articles)
    for label, count in counts.items():
        if count > 1:
            warnings.append(f"article {label} appears {count} times")

    found = {article.number for article in articles}
    highest = max(found, default=0)
    missing = [n for n in range(1, highest + 1) if source.keeps(n) and n not in found]
    if missing:
        warnings.append(f"articles in scope not found: {missing}")
    return warnings


def to_markdown(source: Source, articles: list[Article], fetched_at: str) -> str:
    lines = [
        "---",
        f"id: {source.id}",
        f"title: {json.dumps(source.title, ensure_ascii=False)}",
        f"issuer: {source.issuer}",
        f"source: {source.url}",
        f"fetched_at: {fetched_at}",
        "---",
        "",
        f"# {source.title}",
        "",
    ]
    for article in articles:
        heading = f"## Artículo {article.label}"
        lines.append(f"{heading}. {article.title}" if article.title else heading)
        lines.append("")
        if article.location:
            lines += [f"_{' › '.join(article.location)}_", ""]
        for paragraph in article.paragraphs:
            lines += [paragraph, ""]
        if article.informes:
            unique = list(dict.fromkeys(article.informes))
            lines += [f"<!-- informes: {', '.join(unique)} -->", ""]
    return "\n".join(lines)


def ingest(source: Source, manifest: dict) -> None:
    if source.structure != "articles":
        print(
            f"{source.id:12s} skipped (structure '{source.structure}' not supported yet)"
        )
        return

    articles = parse_articles(read_blocks(source))
    for warning in check_articles(source, articles):
        print(f"{source.id:12s} WARNING {warning}")

    kept = [article for article in articles if source.keeps(article.number)]
    fetched_at = manifest.get(source.id, {}).get("fetched_at", "unknown")
    markdown = to_markdown(source, kept, fetched_at)

    settings.knowledge_base_dir.mkdir(parents=True, exist_ok=True)
    out_path = settings.knowledge_base_dir / f"{source.id}.md"
    out_path.write_text(markdown, encoding="utf-8")
    print(
        f"{source.id:12s} {len(articles)} articles found, {len(kept)} kept "
        f"-> {out_path.name} ({len(markdown):,} chars)"
    )


def main() -> None:
    manifest_path = settings.raw_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for source in load_sources():
        ingest(source, manifest)


if __name__ == "__main__":
    main()
