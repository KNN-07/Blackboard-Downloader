"""Offline exports of available Blackboard BBML and browser link metadata."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit


@dataclass(frozen=True)
class ExportedFile:
    url: str
    filename: str
    extension: str
    content: bytes


_EMBEDDED_PREFIX = "@X@EmbeddedFile.requestUrlStub@X@"
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]|%(?:0[0-9a-f]|1[0-9a-f]|7f)", re.I)


def resolve_content_url(value: str, base_url: str) -> str:
    """Resolve browser hrefs, including Learn's documented embedded-file token."""
    if not isinstance(value, str) or not value or _CONTROLS.search(value):
        return ""
    value = value.strip()
    if not value or "\\" in value:
        return ""
    try:
        base = urlsplit(base_url)
        if value.startswith(_EMBEDDED_PREFIX):
            value = urlunsplit((base.scheme, base.netloc, "/", "", "")) + value[len(_EMBEDDED_PREFIX):].lstrip("/")
        result = urljoin(base_url, value)
        parsed = urlsplit(result)
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or _CONTROLS.search(result) or "\\" in result
                or any(char.isspace() for char in parsed.netloc)):
            return ""
        parsed.port  # Reject invalid ports rather than exporting a broken shortcut.
        return result
    except ValueError:
        return ""


def content_source_url(base_url: str, course_id: str, item: dict) -> str:
    """Prefer advertised alternate links; fallback is a browser link, not a REST API."""
    for link in item.get("links") or ():
        if isinstance(link, dict) and link.get("rel") == "alternate":
            target = resolve_content_url(link.get("href", ""), base_url)
            if target:
                return target
    if not course_id or not item.get("id"):
        return ""
    query = urlencode({"course_id": course_id, "content_id": item["id"]})
    return resolve_content_url("/webapps/blackboard/execute/content?" + query, base_url)


def _escape(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def _destination(url: str) -> str:
    # Angle destinations permit parentheses; encode characters that close them.
    return "<" + quote(url, safe="/:?&=#%+;,@!$'()*[]~-._") + ">"


def _file_anchor(anchor, url: str) -> bool:
    from .client import detected_extension, is_downloadable_extension

    info = {}
    try:
        info = json.loads(anchor.get("data-bbfile", "{}"))
    except (ValueError, TypeError):
        pass
    if not isinstance(info, dict):
        info = {}
    mime = str(info.get("mimeType") or anchor.get("type") or "").split(";", 1)[0].lower()
    names = (info.get("linkName"), info.get("displayName"), anchor.get("download"),
             anchor.get_text(" ", strip=True), urlsplit(url).path)
    if mime and mime not in {"text/html", "application/xhtml+xml"}:
        return True
    if info or anchor.has_attr("download"):
        return True
    return any(is_downloadable_extension(detected_extension(str(name or ""))) for name in names)


class _Markdown:
    """Small structural BBML renderer; unknown containers retain their children."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def render(self, node) -> str:
        from bs4 import NavigableString

        if isinstance(node, NavigableString):
            return _escape(re.sub(r"\s+", " ", str(node)))
        tag = node.name or ""
        children = lambda: "".join(self.render(child) for child in node.children)
        if tag in {"script", "style", "iframe", "object", "embed", "template"}:
            return ""
        if tag == "br":
            return "\n"
        if tag == "hr":
            return "\n\n---\n\n"
        math = node.get("data-mathml") or node.get("data-latex")
        if tag == "math" or math:
            alt = node.get("alttext") or node.get("alt") or node.get("aria-label")
            if not alt and math:
                from bs4 import BeautifulSoup
                alt = BeautifulSoup(math, "html.parser").get_text(" ", strip=True)
            return _escape(alt or node.get_text(" ", strip=True))
        if tag == "img":
            label = _escape(node.get("alt") or node.get("title") or "Image")
            url = resolve_content_url(node.get("src", ""), self.base_url)
            return f"![{label}]({_destination(url)})" if url else label
        if tag == "a":
            label = children().strip()
            url = resolve_content_url(node.get("href", ""), self.base_url)
            return f"[{label or _escape(url)}]({_destination(url)})" if url else label
        if tag == "pre":
            text = node.get_text().strip("\n")
            fence = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", text)), default=3))
            return f"\n\n{fence}\n{text}\n{fence}\n\n"
        if tag == "code":
            text = node.get_text().replace("\n", " ")
            fence = "`" * max(1, max((len(m.group()) + 1 for m in re.finditer(r"`+", text)), default=1))
            padding = " " if text.startswith(("`", " ")) or text.endswith(("`", " ")) else ""
            return f"{fence}{padding}{text}{padding}{fence}"
        if tag in {"ul", "ol"}:
            lines = []
            try:
                start = int(node.get("start", 1))
            except (ValueError, TypeError):
                start = 1
            for index, entry in enumerate(node.find_all("li", recursive=False), start):
                marker = f"{index}. " if tag == "ol" else "- "
                text = self.render(entry).strip()
                parts = text.splitlines() or [""]
                lines.append(marker + parts[0])
                lines.extend(" " * len(marker) + line for line in parts[1:])
            return "\n\n" + "\n".join(lines) + "\n\n"
        if tag == "table":
            rows = []
            for row in node.find_all("tr"):
                if row.find_parent("table") is not node:
                    continue
                cells = [re.sub(r"(?<!\\)\|", r"\\|", self.render(cell).strip().replace("\n", "<br>"))
                         for cell in row.find_all(["td", "th"], recursive=False)]
                if cells:
                    rows.append(cells)
            if not rows:
                return children()
            width = max(map(len, rows))
            rows = [row + [""] * (width - len(row)) for row in rows]
            lines = ["| " + " | ".join(row) + " |" for row in rows]
            lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
            caption = node.find("caption", recursive=False)
            return "\n\n" + (self.render(caption).strip() + "\n\n" if caption else "") + "\n".join(lines) + "\n\n"
        text = children()
        if tag in {"strong", "b", "em", "i", "s", "del"}:
            marker = {"strong": "**", "b": "**", "em": "*", "i": "*", "s": "~~", "del": "~~"}[tag]
            if not text.strip():
                return text
            leading = text[:len(text) - len(text.lstrip())]
            trailing = text[len(text.rstrip()):]
            return leading + marker + text.strip() + marker + trailing
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return "\n\n" + "#" * int(tag[1]) + " " + text.strip() + "\n\n"
        if tag == "blockquote":
            return "\n\n" + "\n".join("> " + line for line in text.strip().splitlines()) + "\n\n"
        if tag in {"p", "div", "section", "article", "figure", "figcaption", "dl", "dt", "dd"}:
            return "\n\n" + text.strip() + "\n\n" if text.strip() else ""
        return text


def export_content(base_url: str, course_id: str, item: dict) -> tuple[ExportedFile, ...]:
    """Export available notes and safe click-throughs, never fetch opaque tool data."""
    from bs4 import BeautifulSoup, Comment
    from .client import safe_name

    title = str(item.get("title") or item.get("name") or "Untitled content")
    source = content_source_url(base_url, course_id, item)
    handler = item.get("contentHandler") or {}
    if not isinstance(handler, dict):
        handler = {}
    handler_id = str(handler.get("id") or "")
    is_tool = "blti" in handler_id.lower() or "lti" in handler_id.lower()
    shortcuts: dict[str, str] = {}
    notes = []
    renderer = _Markdown(base_url)
    seen_bodies = set()
    for field in ("body", "description"):
        body = item.get(field)
        if not isinstance(body, str) or not body.strip() or body in seen_bodies:
            continue
        seen_bodies.add(body)
        soup = BeautifulSoup(body, "html.parser")
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()
        for unwanted in reversed(soup.find_all(["script", "style", "template"])):
            unwanted.decompose()
        for embedded in reversed(soup.find_all(["iframe", "object", "embed"])):
            target = resolve_content_url(
                embedded.get("data" if embedded.name == "object" else "src", ""), base_url
            )
            if target and not _file_anchor(embedded, target):
                shortcuts.setdefault(target, embedded.get("title") or embedded.get("aria-label") or title)
            embedded.decompose()
        file_anchors = []
        for anchor in soup.find_all("a"):
            target = resolve_content_url(anchor.get("href", ""), base_url)
            if _file_anchor(anchor, target):
                file_anchors.append(anchor)
            elif target and not str(anchor.get("href", "")).startswith("#"):
                shortcuts.setdefault(target, anchor.get_text(" ", strip=True) or title)
        rendered = renderer.render(soup).strip()
        # Keep attachment links inside actual instructions, but not redundant notes.
        for anchor in file_anchors:
            anchor.decompose()
        meaningful = soup.get_text(" ", strip=True) or soup.find(["img", "math"]) or soup.find(attrs={"data-mathml": True})
        if meaningful and rendered:
            notes.append(rendered)

    target = resolve_content_url(handler.get("url") or item.get("url") or "", base_url)
    is_file = handler_id == "resource/x-bb-file" or item.get("type") == "File"
    if target and not is_file:
        shortcuts.setdefault(source if is_tool else target, title)
    metadata = []
    for key, label in (("targetType", "Target type"), ("targetId", "Target ID"), ("discussionId", "Discussion ID")):
        if handler.get(key) is not None:
            metadata.append(f"- {label}: {_escape(str(handler[key]))}")
    ordinary = {"", "resource/x-bb-document", "resource/x-bb-folder", "resource/x-bb-file", "resource/x-bb-externallink"}
    opaque = handler_id not in ordinary
    if opaque or metadata:
        if handler_id:
            metadata.insert(0, "- Content type: " + _escape(handler_id))
        if is_tool and target:
            metadata.append("- Advertised tool URL (not an authenticated launch): " + _escape(target))
        notes.append("## Linked content\n\n" + "\n".join(metadata)
                     + "\n\nOpen the Blackboard source to view or launch this content; its contents were not downloaded.")
        if source:
            shortcuts.setdefault(source, title)
    elif handler_id == "resource/x-bb-externallink" and not target and source:
        shortcuts.setdefault(source, title)
    elif not notes and not is_file and any(
        isinstance(link, dict) and link.get("rel") == "alternate"
        for link in item.get("links") or ()
    ) and source:
        shortcuts.setdefault(source, title)

    files = []
    stem = safe_name(title)
    if notes:
        header = "# " + _escape(title) + "\n\n"
        if source:
            header += "[Blackboard source](" + _destination(source) + ")\n\n"
        files.append(ExportedFile(source, stem + ".md", ".md", (header + "\n\n".join(notes) + "\n").encode("utf-8")))
    used_names: set[str] = set()
    for target, label in shortcuts.items():
        if not target:
            continue
        name = safe_name(label)
        candidate = name + ".url"
        index = 2
        while candidate.casefold() in used_names:
            candidate = f"{name} ({index}).url"
            index += 1
        used_names.add(candidate.casefold())
        content = f"[InternetShortcut]\r\nURL={target}\r\n".encode("utf-8")
        files.append(ExportedFile(target, candidate, ".url", content))
    return tuple(files)
