"""Turn the raw documents in data/raw/ into clean Markdown in knowledge_base/.

For laws and regulations (structure = "articles"):
    1. decode     with the encoding recorded in sources.toml (not the server's)
    2. blocks     cut the HTML at every block element, so text split by Word
                  into many <span>s is joined back into whole paragraphs
    3. articles   detect "Artículo 49º.-" headings and group the paragraphs
    4. metadata   pull SUNAT report references out of the text
    5. scope      keep only the articles listed in keep_articles
    6. write      knowledge_base/<id>.md, one "## Artículo" per article

For SUNAT procedures (structure = "procedure"), steps 1-2 are the same; then:
    3. sections   detect "VII. DESCRIPCIÓN", "A. TRAMITACIÓN DEL RÉGIMEN" and
                  "A.1 Numeración de la declaración" headings
    4. clean      drop derogated numerals, attach "(RS N° ...)" notes to the
                  paragraph they modify
    5. write      knowledge_base/<id>.md, one "## VII.A.1" per section

Usage:
    uv run python -m tech_import_assistant.indexing.ingest
"""

import json
import re
from collections import Counter

from bs4 import BeautifulSoup

from tech_import_assistant.config import settings
from tech_import_assistant.indexing.download import load_sources, raw_path
from tech_import_assistant.schemas import Article, ProcedureSection, Source

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

# Procedures. Headings are written in capitals ("VII. DESCRIPCIÓN"); a list
# item such as "I. Despacho anticipado: ..." has lowercase letters, so it is
# not a heading.
ROMAN_NUMERALS = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
PART_HEADING = re.compile(r"^([IVX]+)\.\s+([^a-z]+)$")  # "VII. DESCRIPCIÓN"
LETTER_HEADING = re.compile(r"^([A-Z])\.\s+([^a-z]+)$")  # "A. TRAMITACIÓN..."
SUBLETTER_HEADING = re.compile(r"^([A-Z])\.(\d+)\s+(.+)$")  # "A.1 Numeración..."
# "(RS N° 238-2025/SUNAT - 14.07.2025)" alone in a block: the change note of
# the paragraph above it.
CHANGE_NOTE = re.compile(r"^\(RS[^)]*\)$")
# "5. Numeral Derogado. (RS ...)", "c) texto eliminado.": no rule left in them.
DEROGATED = re.compile(
    r"^\S+\s+(?:numeral|subliteral|texto)\s+(?:derogado|eliminado)\b", re.IGNORECASE
)


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


# --- 3-4. Group blocks into procedure sections ------------------------------


def parse_procedure(blocks: list[str]) -> list[ProcedureSection]:
    """Group the blocks under their deepest heading: part, letter or subletter.

    Headings are accepted only in sequence (I, II, III...; A, B, C... inside a
    part; A.1, A.2... inside a letter). That tells the part "I. OBJETIVO" from
    the letter "I. CANAL DE CONTROL" inside part VI.
    """
    sections: list[ProcedureSection] = []
    part = letter = ""  # "VII. DESCRIPCIÓN", "A. TRAMITACIÓN DEL RÉGIMEN"
    part_number, letter_code, subletter_number = "", "", 0

    for block in blocks:
        next_part = ROMAN_NUMERALS[
            ROMAN_NUMERALS.index(part_number) + 1 if part_number else 0
        ]
        next_letter = chr(ord(letter_code) + 1) if letter_code else "A"

        heading = PART_HEADING.match(block)
        if heading and heading.group(1) == next_part:
            part_number, letter_code, subletter_number = next_part, "", 0
            part, letter = block, ""
            sections.append(
                ProcedureSection(
                    label=part_number,
                    title=heading.group(2),
                    location=[],
                    paragraphs=[],
                )
            )
            continue

        if not sections:
            continue  # page header before "I. OBJETIVO"

        heading = LETTER_HEADING.match(block)
        if heading and heading.group(1) == next_letter:
            letter_code, subletter_number = next_letter, 0
            letter = block
            sections.append(
                ProcedureSection(
                    label=f"{part_number}.{letter_code}",
                    title=heading.group(2),
                    location=[part],
                    paragraphs=[],
                )
            )
            continue

        heading = SUBLETTER_HEADING.match(block)
        if (
            heading
            and heading.group(1) == letter_code
            and int(heading.group(2)) == subletter_number + 1
        ):
            subletter_number += 1
            sections.append(
                ProcedureSection(
                    label=f"{part_number}.{letter_code}.{subletter_number}",
                    title=heading.group(3),
                    location=[part, letter],
                    paragraphs=[],
                )
            )
            continue

        current = sections[-1]
        if DEROGATED.match(block):
            continue
        if CHANGE_NOTE.match(block) and current.paragraphs:
            current.paragraphs[-1] += f" {block}"
            continue
        current.paragraphs.append(block)

    # "VII. DESCRIPCIÓN" goes straight into "A.", and a derogated subletter
    # has no text: neither has anything to retrieve.
    return [section for section in sections if section.paragraphs]


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


def front_matter(source: Source, fetched_at: str) -> list[str]:
    return [
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


def articles_to_markdown(
    source: Source, articles: list[Article], fetched_at: str
) -> str:
    lines = front_matter(source, fetched_at)
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


def procedure_to_markdown(
    source: Source, sections: list[ProcedureSection], fetched_at: str
) -> str:
    lines = front_matter(source, fetched_at)
    for section in sections:
        lines += [f"## {section.label} {section.title}", ""]
        if section.location:
            lines += [f"_{' › '.join(section.location)}_", ""]
        for paragraph in section.paragraphs:
            lines += [paragraph, ""]
    return "\n".join(lines)


def ingest(source: Source, manifest: dict) -> None:
    blocks = read_blocks(source)
    fetched_at = manifest.get(source.id, {}).get("fetched_at", "unknown")

    if source.structure == "articles":
        articles = parse_articles(blocks)
        for warning in check_articles(source, articles):
            print(f"{source.id:12s} WARNING {warning}")
        kept = [article for article in articles if source.keeps(article.number)]
        markdown = articles_to_markdown(source, kept, fetched_at)
        summary = f"{len(articles)} articles found, {len(kept)} kept"
    else:
        sections = parse_procedure(blocks)
        found = {section.label for section in sections}
        unknown = [
            s
            for s in source.skip_sections
            if not any(label == s or label.startswith(f"{s}.") for label in found)
        ]
        if unknown:  # a typo, or SUNAT renumbered the procedure
            print(f"{source.id:12s} WARNING skip_sections not found: {unknown}")
        kept = [section for section in sections if not source.skips(section.label)]
        markdown = procedure_to_markdown(source, kept, fetched_at)
        summary = f"{len(sections)} sections found, {len(kept)} kept"

    settings.knowledge_base_dir.mkdir(parents=True, exist_ok=True)
    out_path = settings.knowledge_base_dir / f"{source.id}.md"
    out_path.write_text(markdown, encoding="utf-8")
    print(f"{source.id:12s} {summary} -> {out_path.name} ({len(markdown):,} chars)")


def main() -> None:
    manifest_path = settings.raw_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for source in load_sources():
        ingest(source, manifest)


if __name__ == "__main__":
    main()
