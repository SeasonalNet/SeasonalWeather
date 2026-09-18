"""Small, non-resolving parser for bounded synthesis markup."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass(frozen=True)
class MarkupElement:
    tag: str
    attributes: dict[str, str]
    text: str


@dataclass
class _Frame:
    tag: str
    attributes: dict[str, str]
    text_parts: list[str] = field(default_factory=list)


class _StrictMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.elements: list[MarkupElement] = []
        self.roots: list[str] = []
        self.outside_text: list[str] = []
        self._stack: list[_Frame] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        attributes: dict[str, str] = {}
        for key, value in attrs:
            normalized_key = key.casefold()
            if value is None or normalized_key in attributes:
                raise ValueError("markup attributes must be unique key/value pairs")
            attributes[normalized_key] = value
        if not self._stack:
            self.roots.append(normalized_tag)
        self._stack.append(_Frame(normalized_tag, attributes))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if not self._stack or self._stack[-1].tag != normalized_tag:
            raise ValueError("markup tags are not correctly nested")
        frame = self._stack.pop()
        self.elements.append(MarkupElement(frame.tag, frame.attributes, "".join(frame.text_parts)))

    def handle_data(self, data: str) -> None:
        if self._stack:
            for frame in self._stack:
                frame.text_parts.append(data)
        else:
            self.outside_text.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        del data
        raise ValueError("markup comments are not permitted")

    def handle_decl(self, decl: str) -> None:
        del decl
        raise ValueError("markup declarations are not permitted")

    def handle_pi(self, data: str) -> None:
        del data
        raise ValueError("markup processing instructions are not permitted")

    def unknown_decl(self, data: str) -> None:
        del data
        raise ValueError("unknown markup declarations are not permitted")

    def finish(self) -> None:
        self.close()
        if self._stack:
            raise ValueError("markup contains an unclosed tag")


def parse_strict_markup(text: str) -> tuple[tuple[str, ...], tuple[MarkupElement, ...], str]:
    """Parse tags without resolving declarations, entities, or external resources."""

    parser = _StrictMarkupParser()
    parser.feed(text)
    parser.finish()
    return tuple(parser.roots), tuple(parser.elements), "".join(parser.outside_text)
