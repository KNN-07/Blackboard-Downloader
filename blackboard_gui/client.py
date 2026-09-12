from __future__ import annotations

import json
import hashlib
import re
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urljoin, urlparse

from .dependencies import install_command
from .content_export import export_content, resolve_content_url

ProgressCallback = Callable[[str, str, int, int], None]


class BlackboardError(RuntimeError):
    """A user-facing Blackboard connection or download error."""


class AuthenticationError(BlackboardError):
    """The stored Blackboard session is missing, invalid, or expired."""


class FileUnavailableError(BlackboardError):
    """A catalogued link could not be accessed; this does not imply deletion."""


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    code: str
    term: str


@dataclass(frozen=True)
class UserProfile:
    id: str
    display_name: str


@dataclass(frozen=True)
class SchoolBranding:
    name: str
    logo_bytes: bytes | None = None


@dataclass(frozen=True)
class RemoteFile:
    url: str
    filename: str
    extension: str = ""
    alternatives: tuple[str, ...] = ()
    identity: str = ""
    source_url: str = ""
    content: bytes | None = None


@dataclass(frozen=True)
class ContentNode:
    id: str
    title: str
    files: tuple[RemoteFile, ...] = ()
    children: tuple[ContentNode, ...] = ()


@dataclass(frozen=True)
class CourseSelection:
    course: Course
    contents: tuple[ContentNode, ...]


MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
    "video/mp4": ".mp4",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/css": ".css",
    "application/json": ".json",
    "application/xml": ".xml",
    "application/javascript": ".js",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "video/webm": ".webm",
}

# Exclude navigation/page assets only for heuristic body links, never explicit files.
IGNORED_WEB_EXTENSIONS = {
    ".action",
    ".asp",
    ".aspx",
    ".cgi",
    ".css",
    ".do",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".jsp",
    ".php",
    ".svg",
    ".xml",
}


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("Enter your Blackboard address.")
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a valid Blackboard address, such as learn.example.edu.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _clean_name(value: str, fallback: str) -> str:
    value = Path(unquote(value or "")).name
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value or fallback


def safe_name(value: str, fallback: str = "Untitled") -> str:
    return _clean_name(value, fallback)[:180]


def compact_name(
    value: str,
    fallback: str = "Untitled",
    max_length: int = 80,
    preserve_extension: bool = False,
) -> str:
    """Shorten a safe path component without making truncated names collide."""
    cleaned = _clean_name(value, fallback)
    if len(cleaned) <= max_length:
        return cleaned
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:8]
    extension = Path(cleaned).suffix if preserve_extension else ""
    room = max(1, max_length - len(extension) - len(digest) - 1)
    return f"{cleaned[:room].rstrip()}~{digest}{extension}"


def bounded_child_path(parent: Path, name: str, directory: bool = False) -> Path:
    """Keep generated course paths below the conservative Windows path ceiling."""
    ceiling = 210 if directory else 240
    default_limit = 80 if directory else 120
    available = ceiling - len(str(parent)) - 1
    if available < 16:
        if directory:
            return parent
        raise BlackboardError(
            "The selected download folder is too deeply nested. Choose a shorter folder path."
        )
    component = compact_name(
        name,
        "Untitled content" if directory else "download",
        min(default_limit, available),
        preserve_extension=not directory,
    )
    return parent / component


def migrate_legacy_file_wrapper(destination: Path) -> bool:
    """Flatten an old app-generated ``file.ext/file.ext`` directory safely."""
    if not destination.is_dir():
        return False
    legacy_file = destination / destination.name
    try:
        contents = list(destination.iterdir())
    except OSError:
        return False
    if contents != [legacy_file] or not legacy_file.is_file():
        return False

    digest = hashlib.sha1(str(destination).encode("utf-8")).hexdigest()[:8]
    backup = destination.parent / f".bb-wrapper-{digest}"
    if backup.exists():
        return False
    try:
        destination.replace(backup)
        (backup / destination.name).replace(destination)
        backup.rmdir()
        return True
    except OSError as exc:
        try:
            if backup.exists() and not destination.exists():
                backup.replace(destination)
        except OSError:
            pass
        raise BlackboardError(
            f"Could not repair the old folder layout for {destination.name}: "
            f"{exc.strerror or exc}"
        ) from exc


def normalize_extensions(values: Iterable[str]) -> set[str]:
    result = set()
    for value in values:
        value = value.strip().lower()
        if value:
            result.add(value if value.startswith(".") else f".{value}")
    return result


def detected_extension(filename: str, mime_type: str = "") -> str:
    """Return the actual suffix advertised by Blackboard, with MIME fallback."""
    extension = Path(filename or "").suffix.lower()
    if extension:
        return extension
    normalized_mime = (mime_type or "").split(";", 1)[0].strip().lower()
    return MIME_EXTENSIONS.get(normalized_mime, "")


def is_downloadable_extension(extension: str) -> bool:
    return bool(extension) and extension.lower() not in IGNORED_WEB_EXTENSIONS


def filter_content_nodes(
    nodes: Iterable[ContentNode], selected_ids: set[str]
) -> tuple[ContentNode, ...]:
    """Keep selected content and the ancestors needed to preserve its folder path."""
    result: list[ContentNode] = []
    for node in nodes:
        children = filter_content_nodes(node.children, selected_ids)
        selected = node.id in selected_ids
        if selected or children:
            result.append(
                ContentNode(
                    id=node.id,
                    title=node.title,
                    files=node.files if selected else (),
                    children=children,
                )
            )
    return tuple(result)


def is_file_wrapper_node(node: ContentNode) -> bool:
    """Identify Blackboard leaf nodes that merely wrap one downloadable file."""
    if node.children or len(node.files) != 1 or node.files[0].content is not None:
        return False
    title = _clean_name(node.title, "Untitled content")
    if is_downloadable_extension(Path(title).suffix.lower()):
        return True
    filename = _clean_name(node.files[0].filename, "download")
    return title.casefold() in {filename.casefold(), Path(filename).stem.casefold()}


def infer_term(course_code: str) -> str | None:
    match = re.match(r"(\d{2})(sprg|fall|sum[123]?)", course_code, re.IGNORECASE)
    if not match:
        return None
    year, semester = match.groups()
    names = {
        "sprg": "Spring",
        "fall": "Fall",
        "sum": "Summer",
        "sum1": "Summer 1",
        "sum2": "Summer 2",
        "sum3": "Summer 3",
    }
    return f"{names.get(semester.lower(), semester.title())} 20{year}"


class BlackboardClient:
    def __init__(self, base_url: str):
        try:
            import requests
        except ImportError as exc:
            raise BlackboardError(
                "Required packages are missing. Run: python -m pip install -r requirements.txt"
            ) from exc
        self.base_url = normalize_base_url(base_url)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Blackboard Downloader/1.0"})
        self.driver = None
        self.browser_branding: SchoolBranding | None = None
        self.request_gate: threading.Semaphore | None = None
        self.parallel_requests = False
        self._thread_session_local = threading.local()
        self._thread_sessions: list = []
        self._thread_sessions_lock = threading.Lock()

    def open_login(self) -> None:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.edge.options import Options as EdgeOptions
            from selenium.webdriver.firefox.options import Options as FirefoxOptions
        except ImportError as exc:
            raise BlackboardError(
                "Browser support is not installed for this Python environment. "
                "Close the app, then run:\n\n"
                f"{install_command()}"
            ) from exc

        errors: list[Exception] = []
        try:
            chrome_options = Options()
            chrome_options.add_argument("--disable-notifications")
            chrome_options.add_argument("--start-maximized")
            self.driver = webdriver.Chrome(options=chrome_options)
        except Exception as exc:
            errors.append(exc)

        if self.driver is None:
            try:
                edge_options = EdgeOptions()
                edge_options.add_argument("--disable-notifications")
                edge_options.add_argument("--start-maximized")
                self.driver = webdriver.Edge(options=edge_options)
            except Exception as exc:
                errors.append(exc)

        if self.driver is None:
            try:
                firefox_options = FirefoxOptions()
                self.driver = webdriver.Firefox(options=firefox_options)
                self.driver.maximize_window()
            except Exception as exc:
                errors.append(exc)

        if self.driver is None:
            detail = str(errors[-1]).splitlines()[0][:240] if errors else "Unknown driver error"
            raise BlackboardError(
                "Selenium could not start Chrome, Microsoft Edge, or Firefox. "
                f"Update an installed browser and try again.\n\nTechnical detail: {detail}"
            )

        try:
            self.driver.get(f"{self.base_url}/ultra")
        except Exception as exc:
            self.close_browser()
            raise BlackboardError(
                "The browser opened, but the Blackboard sign-in page could not be loaded. "
                "Check the Blackboard address and your internet connection."
            ) from exc

    def capture_login(self) -> UserProfile:
        if self.driver is None:
            raise BlackboardError("Open the sign-in page first.")
        try:
            self.browser_branding = self._capture_browser_branding()
            for cookie in self.driver.get_cookies():
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            try:
                agent = self.driver.execute_script("return navigator.userAgent")
                if agent:
                    self.session.headers["User-Agent"] = agent
            except Exception:
                pass
        finally:
            self.close_browser()

        return self.get_current_user()

    def restore_session(self, cookies: list[dict]) -> UserProfile:
        for cookie in cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
                secure=bool(cookie.get("secure", False)),
                expires=cookie.get("expires"),
            )
        return self.get_current_user()

    def export_session(self) -> list[dict]:
        return [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }
            for cookie in self.session.cookies
        ]

    def get_current_user(self) -> UserProfile:
        response = self.session.get(
            f"{self.base_url}/learn/api/public/v1/users/me", timeout=30
        )
        if response.status_code in {401, 403}:
            raise AuthenticationError(
                "Blackboard did not recognize the sign-in. Please try again."
            )
        if response.status_code != 200:
            raise BlackboardError(
                f"Blackboard returned HTTP {response.status_code} while checking the sign-in."
            )
        user = response.json()
        user_id = user.get("id")
        if not user_id:
            raise BlackboardError("The signed-in Blackboard user could not be identified.")
        name = user.get("name") or {}
        parts = [name.get("given"), name.get("middle"), name.get("family")]
        display_name = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        if not display_name:
            display_name = user.get("userName") or "Blackboard user"
        return UserProfile(id=user_id, display_name=display_name)

    def get_school_branding(self) -> SchoolBranding:
        if self.browser_branding and self.browser_branding.logo_bytes:
            return self.browser_branding
        fallback = self._school_name_from_host()
        try:
            from bs4 import BeautifulSoup

            response = self.session.get(self.base_url, timeout=20)
            if response.status_code != 200:
                return SchoolBranding(fallback)
            soup = BeautifulSoup(response.text, "html.parser")
            school_name = self._school_name_from_page(soup) or fallback
            icon_url = None
            for link in soup.find_all("link"):
                rel = link.get("rel") or []
                rel_values = [str(value).lower() for value in rel]
                if any("icon" in value for value in rel_values) and link.get("href"):
                    icon_url = urljoin(self.base_url, link.get("href"))
                    break
            logo_bytes = None
            if icon_url:
                icon_response = self.session.get(icon_url, timeout=15)
                if icon_response.status_code == 200 and len(icon_response.content) <= 2_000_000:
                    logo_bytes = icon_response.content
            return SchoolBranding(school_name, logo_bytes)
        except Exception:
            return SchoolBranding(fallback)

    def _capture_browser_branding(self) -> SchoolBranding | None:
        """Capture Blackboard's rendered institution logo before closing Selenium."""
        if self.driver is None:
            return None
        fallback = self._school_name_from_host()
        selectors = (
            "div.MuiDrawer-root.MuiDrawer-anchorLeft header > a > img",
            '[class*="makeStyleslogoHref"]',
            '[class*="makeStyleslogoContainer"]',
        )
        for selector in selectors:
            try:
                elements = self.driver.find_elements("css selector", selector)
            except Exception:
                continue
            for element in elements:
                try:
                    if not element.is_displayed():
                        continue
                    width = element.size.get("width", 0)
                    height = element.size.get("height", 0)
                    if width < 8 or height < 8:
                        continue
                    candidates = [
                        element.get_attribute("aria-label"),
                        element.get_attribute("title"),
                        element.get_attribute("alt"),
                    ]
                    for image in element.find_elements("css selector", "img"):
                        candidates.extend(
                            [
                                image.get_attribute("alt"),
                                image.get_attribute("title"),
                            ]
                        )
                    school_name = fallback
                    for candidate in candidates:
                        if not candidate or not candidate.strip():
                            continue
                        cleaned = re.sub(
                            r"\s+(?:school\s+)?logo$", "", candidate.strip(), flags=re.I
                        )
                        if cleaned.lower() not in {"logo", "home", "blackboard"}:
                            school_name = cleaned
                            break
                    logo_bytes = element.screenshot_as_png
                    if logo_bytes and len(logo_bytes) <= 2_000_000:
                        return SchoolBranding(school_name, logo_bytes)
                except Exception:
                    continue
        return None

    def _school_name_from_host(self) -> str:
        host = (urlparse(self.base_url).hostname or "Blackboard").lower()
        ignored = {
            "www",
            "learn",
            "lms",
            "blackboard",
            "bb",
            "courses",
            "online",
            "elearning",
            "ntulearn",
            "edu",
            "ac",
            "com",
            "org",
            "net",
            "sg",
            "uk",
        }
        candidates = [part for part in host.split(".") if part not in ignored]
        name = candidates[0] if candidates else host.split(".")[0]
        return name.upper() if len(name) <= 5 else name.replace("-", " ").title()

    @staticmethod
    def _school_name_from_page(soup) -> str | None:
        for attrs in (
            {"property": "og:site_name"},
            {"name": "application-name"},
            {"name": "apple-mobile-web-app-title"},
        ):
            meta = soup.find("meta", attrs=attrs)
            content = meta.get("content", "").strip() if meta else ""
            if content and "blackboard" not in content.lower():
                return content
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if title and title.lower() not in {"blackboard", "blackboard learn"}:
            for separator in (" | ", " — ", " – ", " - "):
                parts = [part.strip() for part in title.split(separator)]
                specific = [part for part in parts if part and "blackboard" not in part.lower()]
                if specific:
                    return specific[0]
            if "blackboard" not in title.lower():
                return title
        return None

    def close_browser(self) -> None:
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def close(self) -> None:
        self.close_browser()
        with self._thread_sessions_lock:
            thread_sessions, self._thread_sessions = self._thread_sessions, []
        for session in thread_sessions:
            session.close()
        self.session.close()

    def create_worker_client(
        self, request_gate: threading.Semaphore | None = None
    ) -> BlackboardClient:
        """Create an isolated authenticated client safe for a background worker."""
        worker = BlackboardClient(self.base_url)
        worker.request_gate = request_gate
        worker.parallel_requests = request_gate is not None
        worker.session.headers.clear()
        worker.session.headers.update(dict(self.session.headers))
        for cookie in self.session.cookies:
            worker.session.cookies.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain,
                path=cookie.path,
                secure=cookie.secure,
                expires=cookie.expires,
            )
        return worker

    def _get(self, url: str, **kwargs):
        """Apply the optional shared request limit used by parallel index workers."""
        session = self.session
        if getattr(self, "parallel_requests", False):
            session = getattr(self._thread_session_local, "session", None)
            if session is None:
                session = self.session.__class__()
                session.headers.clear()
                session.headers.update(dict(self.session.headers))
                for cookie in self.session.cookies:
                    session.cookies.set(
                        cookie.name,
                        cookie.value,
                        domain=cookie.domain,
                        path=cookie.path,
                        secure=cookie.secure,
                        expires=cookie.expires,
                    )
                self._thread_session_local.session = session
                with self._thread_sessions_lock:
                    self._thread_sessions.append(session)
        request_gate = getattr(self, "request_gate", None)
        if request_gate is None:
            return session.get(url, **kwargs)
        with request_gate:
            return session.get(url, **kwargs)

    def get_courses(self, user_id: str, progress: ProgressCallback) -> list[Course]:
        enrollments = self._paged_json(
            f"{self.base_url}/learn/api/public/v1/users/{user_id}/courses?limit=100"
        )
        courses: list[Course] = []
        total = len(enrollments)
        for index, enrollment in enumerate(enrollments, 1):
            course_id = enrollment.get("courseId")
            if not course_id:
                continue
            progress("status", "Reading your courses…", index, total)
            response = self.session.get(
                f"{self.base_url}/learn/api/public/v1/courses/{course_id}", timeout=30
            )
            if response.status_code != 200:
                continue
            detail = response.json()
            code = detail.get("courseId", "")
            courses.append(
                Course(
                    id=course_id,
                    name=detail.get("name") or "Unnamed course",
                    code=code,
                    term=detail.get("term", {}).get("name")
                    or infer_term(code)
                    or "Other courses",
                )
            )
        return sorted(courses, key=lambda course: (course.term.lower(), course.name.lower()))

    def get_course_contents(
        self,
        courses: list[Course],
        progress: ProgressCallback,
        cancel: threading.Event,
    ) -> tuple[dict[str, tuple[ContentNode, ...]], dict[str, str]]:
        """Build the content tree once so the UI and downloader share one catalog."""
        catalog: dict[str, tuple[ContentNode, ...]] = {}
        errors: dict[str, str] = {}
        total = len(courses)
        for index, course in enumerate(courses, 1):
            self._check_cancel(cancel)
            progress("status", f"Indexing {course.name}…", index - 1, total)
            try:
                catalog[course.id] = self.get_course_content(course, cancel)
            except AuthenticationError:
                raise
            except BlackboardError as exc:
                catalog[course.id] = ()
                errors[course.id] = str(exc)
        return catalog, errors

    def get_course_content(
        self, course: Course, cancel: threading.Event
    ) -> tuple[ContentNode, ...]:
        """Index one course with parallel discovery of independent content items."""
        roots = self._paged_json(
            f"{self.base_url}/learn/api/public/v1/courses/{course.id}/contents?limit=100"
        )
        records: dict[str, tuple[str, tuple[RemoteFile, ...], tuple[str, ...]]] = {}
        scheduled: set[str] = set()
        root_ids = tuple(item.get("id", "") for item in roots if item.get("id"))

        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="bb-item") as executor:
            pending = {}

            def schedule(item: dict) -> None:
                item_id = item.get("id", "")
                if not item_id or item_id in scheduled:
                    return
                scheduled.add(item_id)
                future = executor.submit(
                    self._read_content_item, course.id, item, cancel
                )
                pending[future] = item_id

            for item in roots:
                schedule(item)

            while pending:
                self._check_cancel(cancel)
                completed, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in completed:
                    pending.pop(future, None)
                    result = future.result()
                    if result is None:
                        continue
                    item_id, title, files, children = result
                    child_ids = tuple(
                        child.get("id", "") for child in children if child.get("id")
                    )
                    records[item_id] = (title, files, child_ids)
                    for child in children:
                        schedule(child)

        def build(item_id: str, ancestry: frozenset[str]) -> ContentNode | None:
            if item_id in ancestry or item_id not in records:
                return None
            title, files, child_ids = records[item_id]
            path = ancestry | {item_id}
            children = tuple(
                node
                for child_id in child_ids
                if (node := build(child_id, path)) is not None
            )
            if not files and not children:
                return None
            return ContentNode(item_id, title, files, children)

        contents = tuple(
            node for item_id in root_ids if (node := build(item_id, frozenset())) is not None
        )
        return contents + self._course_extras(course.id, cancel)

    def _item_files(self, course_id: str, item: dict, source_url: str) -> list[RemoteFile]:
        files = [
            replace(remote, source_url=source_url)
            for field in ("body", "description")
            for remote in self._extract_body_files(item.get(field, ""), None)
        ]
        files.extend(
            RemoteFile(export.url, export.filename, export.extension,
                       identity=f"{source_url}#export:{index}", source_url=source_url,
                       content=export.content)
            for index, export in enumerate(export_content(self.base_url, course_id, item))
        )
        return self._merge_files(files)

    def _course_extras(self, course_id: str, cancel: threading.Event) -> tuple[ContentNode, ...]:
        base = f"{self.base_url}/learn/api/public/v1/courses/{course_id}"
        visited: set[str] = set()

        def resource_node(item: dict) -> ContentNode | None:
            self._check_cancel(cancel)
            resource_id = str(item.get("id") or "")
            if not resource_id or resource_id in visited:
                return None
            visited.add(resource_id)
            source = f"{base}/resources/{resource_id}"
            if item.get("type") == "File" and not item.get("downloadUrl"):
                detail = self._get(source, timeout=30)
                try:
                    if detail.status_code == 401:
                        raise AuthenticationError("Your Blackboard session expired. Sign in again.")
                    if detail.status_code == 200:
                        item = {**item, **detail.json()}
                finally:
                    detail.close()
            children = ()
            if item.get("type") == "Folder":
                children = tuple(
                    node for child in self._paged_json(f"{source}/children?limit=100", missing_ok=True)
                    if (node := resource_node(child)) is not None
                )
            files = []
            url = resolve_content_url(item.get("downloadUrl", ""), self.base_url)
            if item.get("type") == "File" and url:
                filename = item.get("name") or "download"
                extension = detected_extension(filename, item.get("mimeType", "")) or ".bin"
                if extension:
                    files.append(RemoteFile(url, filename, extension, identity=source, source_url=source))
            if not files and not children:
                return None
            return ContentNode(f"resource:{resource_id}", item.get("name") or "Resource",
                               tuple(files), children)

        resources = tuple(
            node for item in self._paged_json(f"{base}/resources?limit=100", missing_ok=True)
            if (node := resource_node(item)) is not None
        )
        announcements = []
        for item in self._paged_json(f"{base}/announcements?limit=100", missing_ok=True):
            self._check_cancel(cancel)
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            source = f"{base}/announcements/{item_id}"
            detail = self._get(source, timeout=30)
            try:
                if detail.status_code == 401:
                    raise AuthenticationError("Your Blackboard session expired. Sign in again.")
                if detail.status_code == 200:
                    item = {**item, **detail.json()}
            finally:
                detail.close()
            if not item.get("links"):
                # Browser course page, not a fabricated announcement REST permalink.
                item = {**item, "links": [{"rel": "alternate", "href":
                        f"{self.base_url}/ultra/courses/{course_id}/announcements"}]}
            files = self._item_files(course_id, item, source)
            if files:
                announcements.append(ContentNode(f"announcement:{item_id}",
                                                 item.get("title") or "Announcement", tuple(files)))
        extras = []
        if resources:
            extras.append(ContentNode("resources", "Course resources", children=resources))
        if announcements:
            extras.append(ContentNode("announcements", "Announcements", children=tuple(announcements)))
        return tuple(extras)

    def _read_content_item(
        self, course_id: str, item: dict, cancel: threading.Event
    ) -> tuple[str, str, tuple[RemoteFile, ...], list[dict]] | None:
        """Read one item without recursing so sibling items can run concurrently."""
        self._check_cancel(cancel)
        item_id = item.get("id", "")
        if not item_id:
            return None
        item_url = (
            f"{self.base_url}/learn/api/public/v1/courses/{course_id}/contents/{item_id}"
        )
        title = item.get("title") or "Untitled content"
        files: list[RemoteFile] = []

        detail = self._get(item_url, timeout=30, params={"includeInActivityTracking": "false"})
        try:
            if detail.status_code == 401:
                raise AuthenticationError("Your Blackboard session expired. Sign in again.")
            detail_data = {**item, **detail.json()} if detail.status_code == 200 else item
        finally:
            detail.close()
        title = detail_data.get("title") or title
        files.extend(self._item_files(course_id, detail_data, item_url))

        for attachment in self._paged_json(
            f"{item_url}/attachments?limit=100", missing_ok=True
        ):
            attachment_id = attachment.get("id")
            if not attachment_id:
                continue
            filename = attachment.get("fileName") or "download"
            extension = detected_extension(filename, attachment.get("mimeType", "")) or ".bin"
            if not extension:
                continue
            download_url = f"{item_url}/attachments/{attachment_id}/download"
            identity = f"{item_url}/attachments/{attachment_id}"
            alternatives = ()
            # Only a returned redirect can connect an attachment ID to a BBML xid.
            if any("/bbcswebdav/xid-" in remote.identity for remote in files):
                redirect = self._get(download_url, stream=True, allow_redirects=False, timeout=30)
                try:
                    if redirect.status_code in {301, 302, 303, 307, 308}:
                        target = resolve_content_url(redirect.headers.get("Location", ""), download_url)
                        target_identity = self._file_identity(target) if target else ""
                        if target_identity and any(remote.identity == target_identity for remote in files):
                            identity = target_identity
                            alternatives = (target,)
                finally:
                    redirect.close()
            files.append(
                RemoteFile(
                    url=download_url,
                    filename=filename,
                    extension=extension,
                    alternatives=alternatives,
                    identity=identity,
                    source_url=item_url,
                )
            )

        children = self._paged_json(
            f"{item_url}/children?limit=100&includeInActivityTracking=false", missing_ok=True
        )
        return item_id, title, tuple(self._merge_files(files)), children

    def _scan_content_node(
        self,
        course_id: str,
        item: dict,
        visited: set[str],
        cancel: threading.Event,
    ) -> ContentNode | None:
        self._check_cancel(cancel)
        item_id = item.get("id", "")
        if not item_id or item_id in visited:
            return None
        visited.add(item_id)
        result = self._read_content_item(course_id, item, cancel)
        if result is None:
            return None
        _, title, files, child_items = result
        children = tuple(
            node
            for child in child_items
            if (node := self._scan_content_node(course_id, child, visited, cancel))
            is not None
        )
        if not files and not children:
            return None
        return ContentNode(
            id=item_id,
            title=title,
            files=tuple(files),
            children=children,
        )

    def download_courses(
        self,
        selections: list[CourseSelection],
        output_dir: Path,
        extensions: set[str],
        progress: ProgressCallback,
        cancel: threading.Event,
        request_gate: threading.Semaphore | None = None,
    ) -> tuple[int, int, int]:
        downloaded = 0
        skipped = 0
        unavailable = 0
        output_dir.mkdir(parents=True, exist_ok=True)
        jobs: dict[Path, RemoteFile] = {}
        candidates: dict[str, list[tuple[Path, RemoteFile]]] = {}
        seen_files: dict[tuple[str, str], tuple[str, int]] = {}

        for selection in selections:
            self._check_cancel(cancel)
            course = selection.course
            course_dir = bounded_child_path(output_dir, course.name, directory=True)
            logged_directories: set[Path] = set()

            def collect_node(node: ContentNode, parent: Path) -> None:
                self._check_cancel(cancel)
                component = compact_name(node.title, "Untitled content", 80)
                if is_file_wrapper_node(node):
                    node_dir = parent
                else:
                    node_dir = (
                        parent
                        if parent.name.casefold() == component.casefold()
                        else bounded_child_path(parent, node.title, directory=True)
                    )
                if node_dir not in logged_directories:
                    logged_directories.add(node_dir)
                    relative = node_dir.relative_to(course_dir)
                    location = course.name if str(relative) == "." else f"{course.name}  /  {relative}"
                    progress("log", location, 0, 0)
                for remote in node.files:
                    self._check_cancel(cancel)
                    if remote.extension not in extensions:
                        continue
                    identity = (str(node_dir).casefold(), remote.identity or remote.url)
                    if identity in seen_files:
                        key, index = seen_files[identity]
                        previous_path, previous = candidates[key][index]
                        candidates[key][index] = (previous_path, self._merge_files((previous, remote))[0])
                        continue
                    filename = _clean_name(remote.filename, "download")
                    if not Path(filename).suffix:
                        filename += remote.extension
                    destination = bounded_child_path(node_dir, filename, directory=False)
                    key = str(destination).casefold()
                    group = candidates.setdefault(key, [])
                    seen_files[identity] = (key, len(group))
                    group.append((destination, remote))

                for child in node.children:
                    collect_node(child, node_dir)

            for root in selection.contents:
                collect_node(root, course_dir)

        used_paths: set[str] = set()
        for group in candidates.values():
            for destination, remote in group:
                if len(group) > 1:
                    digest = hashlib.sha256((remote.identity or remote.url).encode()).hexdigest()[:12]
                    destination = bounded_child_path(
                        destination.parent,
                        f"{destination.stem}~{digest}{destination.suffix}",
                    )
                counter = 0
                original = destination
                while str(destination).casefold() in used_paths:
                    counter += 1
                    digest = hashlib.sha256(f"{remote.identity or remote.url}:{counter}".encode()).hexdigest()[:16]
                    destination = bounded_child_path(original.parent, f"{original.stem}~{digest}{original.suffix}")
                used_paths.add(str(destination).casefold())
                jobs[destination] = remote

        total_files = len(jobs)
        if not total_files:
            progress("file", "No matching files to download", 0, 0)
            return downloaded, skipped, unavailable

        progress("file", f"Downloading {total_files} file(s)…", 0, total_files)
        worker_local = threading.local()
        worker_clients: list[BlackboardClient] = []
        worker_clients_lock = threading.Lock()

        def worker_client() -> BlackboardClient:
            worker = getattr(worker_local, "client", None)
            if worker is None:
                worker = self.create_worker_client()
                worker_local.client = worker
                with worker_clients_lock:
                    worker_clients.append(worker)
            return worker

        def transfer(
            destination: Path, remote: RemoteFile
        ) -> tuple[str, Path, str]:
            self._check_cancel(cancel)
            if destination.is_dir():
                if migrate_legacy_file_wrapper(destination):
                    return "existing", destination, ""
                raise BlackboardError(
                    f"A folder named {destination.name} blocks that file's download. "
                    "Rename or remove the folder, then try again."
                )
            if destination.exists():
                return "existing", destination, ""
            if request_gate is not None:
                request_gate.acquire()
            try:
                self._check_cancel(cancel)
                try:
                    saved, was_downloaded = worker_client()._download_remote(
                        remote, destination, cancel
                    )
                except FileUnavailableError as exc:
                    return "unavailable", destination, str(exc)
                return ("downloaded" if was_downloaded else "existing"), saved, ""
            finally:
                if request_gate is not None:
                    request_gate.release()

        completed = 0
        try:
            with ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="bb-download"
            ) as executor:
                futures = {
                    executor.submit(transfer, destination, remote): destination
                    for destination, remote in jobs.items()
                }
                for future in as_completed(futures):
                    self._check_cancel(cancel)
                    status, saved, diagnostic = future.result()
                    completed += 1
                    if status == "downloaded":
                        downloaded += 1
                        progress("log", f"Downloaded: {saved.name}", 0, 0)
                    elif status == "existing":
                        skipped += 1
                        progress("log", f"Skipped existing: {saved.name}", 0, 0)
                    else:
                        unavailable += 1
                        progress(
                            "log",
                            f"Link inaccessible (not necessarily deleted): {saved.name}: {diagnostic}",
                            0,
                            0,
                        )
                    progress(
                        "file",
                        f"Processed {completed} of {total_files} file(s)",
                        completed,
                        total_files,
                    )
        finally:
            for worker in worker_clients:
                worker.close()
        return downloaded, skipped, unavailable

    def _paged_json(self, url: str, missing_ok: bool = False) -> list[dict]:
        results: list[dict] = []
        visited: set[str] = set()
        while url and url not in visited:
            visited.add(url)
            response = self._get(url, timeout=30)
            try:
                # Optional collections may be unavailable to a handler or this user.
                if missing_ok and response.status_code in {400, 403, 404, 405, 501}:
                    return results
                if response.status_code in {401, 403}:
                    raise AuthenticationError(
                        "Your Blackboard session expired or this collection is not permitted."
                    )
                if response.status_code != 200:
                    raise BlackboardError(
                        f"Blackboard returned HTTP {response.status_code} while loading data."
                    )
                try:
                    data = response.json()
                except ValueError as exc:
                    raise BlackboardError(
                        "Blackboard returned an unreadable response. Your session may have expired."
                    ) from exc
            finally:
                response.close()
            results.extend(data.get("results", []))
            next_page = data.get("paging", {}).get("nextPage")
            url = resolve_content_url(next_page, url) if next_page else ""
            if url and urlparse(url).netloc != urlparse(self.base_url).netloc:
                raise BlackboardError("Blackboard supplied an off-site collection page.")
        return results


    def _file_identity(self, url: str) -> str:
        parsed = urlparse(url)
        origin = urlparse(self.base_url)
        if (parsed.scheme, parsed.netloc.casefold()) == (origin.scheme, origin.netloc.casefold()):
            match = re.search(r"/bbcswebdav/(?:[^?#]*/)?xid-(\d+_\d+)/?$", parsed.path)
            if match:
                return f"{self.base_url}/bbcswebdav/xid-{match.group(1)}"
        return url

    @staticmethod
    def _merge_files(files: Iterable[RemoteFile]) -> list[RemoteFile]:
        merged: dict[str, RemoteFile] = {}
        for remote in files:
            key = remote.identity or remote.url
            previous = merged.get(key)
            if previous is None:
                merged[key] = remote
            else:
                alternatives = tuple(dict.fromkeys(
                    value for value in (*previous.alternatives, remote.url, *remote.alternatives)
                    if value != previous.url
                ))
                merged[key] = replace(previous, alternatives=alternatives)
        return list(merged.values())

    def _extract_body_files(
        self, body: str, extensions: set[str] | None
    ) -> list[RemoteFile]:
        if not isinstance(body, str) or not body:
            return []
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:
            raise BlackboardError(
                "Beautiful Soup is missing. Run: python -m pip install -r requirements.txt"
            ) from exc
        files: list[RemoteFile] = []
        soup = BeautifulSoup(body, "html.parser")
        for element in soup.find_all(["a", "img", "audio", "video", "source", "object"]):
            try:
                info = json.loads(element.get("data-bbfile", "{}"))
                if not isinstance(info, dict):
                    info = {}
            except (ValueError, TypeError):
                info = {}
            advertised = element.get("href") if element.name == "a" else element.get(
                "data" if element.name == "object" else "src"
            )
            url = resolve_content_url(advertised or "", self.base_url)
            resource = resolve_content_url(info.get("resourceUrl", ""), self.base_url)
            url = url or resource
            if not url:
                continue
            path_name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
            mime = info.get("mimeType") or element.get("type") or ""
            if not isinstance(mime, str):
                mime = ""
            explicit = bool(info) or element.has_attr("download") or element.name != "a"
            candidates = [
                info.get("linkName"), info.get("displayName"), element.get("download"),
                element.get_text(" ", strip=True), element.get("title"), path_name,
            ]
            filename = ""
            for index, name in enumerate(candidates):
                if not isinstance(name, str):
                    continue
                suffix = detected_extension(name)
                if not suffix or (not explicit and not is_downloadable_extension(suffix)):
                    continue
                if index >= 3 and suffix in {".action", ".asp", ".aspx", ".cgi", ".do", ".jsp", ".php"}:
                    continue
                filename = name
                break
            identity = self._file_identity(url)
            is_xythos = identity != url or "/bbcswebdav/xid-" in identity
            extension = detected_extension(filename, mime)
            if not extension and is_xythos:
                extension = ".bin"
            if not extension or (not explicit and not is_downloadable_extension(extension)):
                continue
            # Server-side navigation is not a file merely because its label has a suffix.
            if detected_extension(path_name) in IGNORED_WEB_EXTENSIONS and not explicit and not mime:
                continue
            if extensions is not None and extension not in extensions:
                continue
            if not filename:
                label = next((name for name in candidates if isinstance(name, str)
                              and name.strip()), "download")
                filename = f"{Path(label).stem or 'download'}{extension}"
            alternatives = (resource,) if resource and resource != url and (
                self._file_identity(resource) == identity
            ) else ()
            files.append(RemoteFile(url, filename, extension, alternatives, identity))
        return self._merge_files(files)

    @staticmethod
    def _matches(filename: str, mime_type: str, extensions: set[str]) -> bool:
        extension = detected_extension(filename, mime_type)
        return is_downloadable_extension(extension) and extension in extensions

    def _refresh_file(self, remote: RemoteFile) -> tuple[str, ...]:
        if not remote.source_url or not remote.identity:
            return ()
        response = self._get(remote.source_url, timeout=30,
                             params={"includeInActivityTracking": "false"}
                             if "/contents/" in remote.source_url else None)
        try:
            if response.status_code != 200:
                return ()
            item = response.json()
        finally:
            response.close()
        if "/resources/" in remote.source_url and remote.identity == remote.source_url:
            url = resolve_content_url(item.get("downloadUrl", ""), self.base_url)
            return (url,) if url else ()
        refreshed = [
            candidate for field in ("body", "description")
            for candidate in self._extract_body_files(item.get(field, ""), None)
            if candidate.identity == remote.identity
        ]
        return tuple(dict.fromkeys(url for candidate in refreshed
                                   for url in (candidate.url, *candidate.alternatives)))

    def _download_remote(
        self, remote: RemoteFile, destination: Path, cancel: threading.Event
    ) -> tuple[Path, bool]:
        from requests import RequestException

        if remote.content is not None:
            self._check_cancel(cancel)
            if destination.exists():
                return destination, False
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = destination.with_name(f"{destination.name}.part")
            try:
                partial.write_bytes(remote.content)
                self._check_cancel(cancel)
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)
            return destination, True
        attempted: set[str] = set()
        errors = []
        urls = (remote.url, *remote.alternatives)
        for refresh in (False, True):
            if refresh:
                self._check_cancel(cancel)
                try:
                    urls = self._refresh_file(remote)
                except (ValueError, RequestException):
                    urls = ()
            for url in urls:
                self._check_cancel(cancel)
                if url in attempted:
                    continue
                attempted.add(url)
                try:
                    return self._download_file(url, destination, cancel)
                except FileUnavailableError as exc:
                    errors.append(str(exc))
                except RequestException:
                    errors.append(f"Network error at {urlparse(url).path}: {destination.name}")
        raise FileUnavailableError("; ".join(errors) or f"No accessible link: {destination.name}")

    def _download_file(
        self, url: str, destination: Path, cancel: threading.Event
    ) -> tuple[Path, bool]:
        response = self._get(url, stream=True, allow_redirects=True, timeout=(30, 120))
        if response.status_code == 401 and urlparse(url).netloc == urlparse(self.base_url).netloc:
            response.close()
            raise AuthenticationError("Your Blackboard session expired. Sign in again.")
        if response.status_code != 200:
            response.close()
            raise FileUnavailableError(
                f"HTTP {response.status_code} at {urlparse(url).path}: {destination.name}"
            )

        # Keep the preallocated name: response filenames can collide across workers.

        if destination.exists():
            response.close()
            return destination, False

        partial = destination.with_name(f"{destination.name}.part")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with partial.open("wb") as handle:
                prefix = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    self._check_cancel(cancel)
                    if chunk:
                        if len(prefix) < 4096:
                            prefix.extend(chunk[:4096 - len(prefix)])
                            start = bytes(prefix).lstrip().lower()
                            html = start.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
                            content_type = response.headers.get("Content-Type", "").lower()
                            if (html or content_type.startswith("text/html")) and destination.suffix.lower() not in {".html", ".htm"}:
                                raise FileUnavailableError(
                                    f"HTML/login page instead of a file at {urlparse(url).path}"
                                )
                            response_url = getattr(response, "url", "")
                            login_route = isinstance(response_url, str) and bool(
                                re.search(r"/(?:login|signon|webapps/login)(?:[/.?]|$)", response_url, re.I)
                            )
                            if (html or content_type.startswith("text/html")) and (
                                login_route or any(marker in start for marker in (
                                    b'type="password"', b"type='password'",
                                    b'name="password"', b"name='password'",
                                ))
                            ):
                                raise FileUnavailableError(f"Login page at {urlparse(url).path}")
                        handle.write(chunk)
            partial.replace(destination)
        except OSError as exc:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            if getattr(exc, "winerror", None) == 206 or getattr(exc, "errno", None) == 36:
                raise BlackboardError(
                    "A download path is too long. Choose a shorter download folder and try again."
                ) from exc
            detail = exc.strerror or str(exc)
            raise BlackboardError(
                f"Could not save {destination.name}: {detail}"
            ) from exc
        except Exception:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            response.close()
        return destination, True

    @staticmethod
    def _check_cancel(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise InterruptedError("Download cancelled")
