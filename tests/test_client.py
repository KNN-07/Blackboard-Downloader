import threading
import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

from blackboard_gui.client import (
    AuthenticationError,
    BlackboardError,
    BlackboardClient,
    ContentNode,
    Course,
    CourseSelection,
    FileUnavailableError,
    RemoteFile,
    compact_name,
    detected_extension,
    filter_content_nodes,
    is_file_wrapper_node,
    migrate_legacy_file_wrapper,
    infer_term,
    normalize_base_url,
    normalize_extensions,
    safe_name,
)


class ClientHelpersTest(unittest.TestCase):
    def test_normalize_base_url(self):
        self.assertEqual(normalize_base_url("learn.example.edu/"), "https://learn.example.edu")
        self.assertEqual(
            normalize_base_url("https://learn.example.edu/ultra/course"),
            "https://learn.example.edu",
        )

    def test_normalize_base_url_rejects_empty_value(self):
        with self.assertRaises(ValueError):
            normalize_base_url("  ")

    def test_safe_name_blocks_windows_path_characters(self):
        self.assertEqual(safe_name('Week 1: Intro?.pdf'), "Week 1_ Intro_.pdf")
        self.assertEqual(safe_name("../notes.pdf"), "notes.pdf")

    def test_normalize_extensions(self):
        self.assertEqual(normalize_extensions(["PDF", ".PPTX", ""]), {".pdf", ".pptx"})

    def test_infer_term(self):
        self.assertEqual(infer_term("26sprgcasma225_c1"), "Spring 2026")
        self.assertIsNone(infer_term("MATH-101"))

    def test_file_matching_accepts_extension_or_mime_type(self):
        self.assertTrue(BlackboardClient._matches("slides.pptx", "", {".pptx"}))
        self.assertTrue(
            BlackboardClient._matches("download", "application/pdf", {".pdf"})
        )
        self.assertFalse(BlackboardClient._matches("notes.txt", "text/plain", {".pdf"}))

    def test_detected_extension_uses_course_filename_then_mime_fallback(self):
        self.assertEqual(detected_extension("worksheet.XLSX", "application/pdf"), ".xlsx")
        self.assertEqual(detected_extension("download", "application/pdf; charset=utf-8"), ".pdf")

    def test_filter_content_nodes_keeps_path_to_selected_descendant(self):
        selected_file = RemoteFile("https://example.test/notes", "notes.pdf", ".pdf")
        tree = (
            ContentNode(
                "root",
                "Week 1",
                files=(RemoteFile("https://example.test/intro", "intro.docx", ".docx"),),
                children=(ContentNode("child", "Lecture", files=(selected_file,)),),
            ),
        )
        filtered = filter_content_nodes(tree, {"child"})
        self.assertEqual(filtered[0].title, "Week 1")
        self.assertEqual(filtered[0].files, ())
        self.assertEqual(filtered[0].children[0].files, (selected_file,))

    def test_file_matching_ignores_blackboard_jsp_routes(self):
        self.assertFalse(BlackboardClient._matches("content.jsp", "", {".jsp"}))
        self.assertTrue(BlackboardClient._matches("lecture-notes.pdf", "", {".pdf"}))

    def test_course_scan_prunes_empty_content_nodes(self):
        client = object.__new__(BlackboardClient)
        client.base_url = "https://learn.example.edu"
        detail = Mock(status_code=200)
        detail.json.return_value = {"title": "Empty folder", "body": ""}
        client.session = Mock()
        client.session.get.return_value = detail
        client._paged_json = Mock(return_value=[])
        node = client._scan_content_node(
            "course", {"id": "empty", "title": "Empty folder"}, set(), threading.Event()
        )
        self.assertIsNone(node)

    def test_single_course_scans_independent_items_in_parallel(self):
        client = object.__new__(BlackboardClient)
        client.base_url = "https://learn.example.edu"
        roots = [{"id": f"item-{index}", "title": f"Item {index}"} for index in range(6)]
        client._paged_json = Mock(side_effect=lambda url, **_kwargs: roots if "/contents?" in url else [])
        condition = threading.Condition()
        release = threading.Event()
        state = {"active": 0, "peak": 0}

        def read_item(_course_id, item, _cancel):
            with condition:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                condition.notify_all()
            release.wait(2)
            with condition:
                state["active"] -= 1
            remote = RemoteFile(f"https://example.test/{item['id']}", "notes.pdf", ".pdf")
            return item["id"], item["title"], (remote,), []

        client._read_content_item = read_item
        result = []
        scan = threading.Thread(
            target=lambda: result.extend(
                client.get_course_content(
                    Course("course", "Course", "CODE", "Term"), threading.Event()
                )
            )
        )
        scan.start()
        with condition:
            condition.wait_for(lambda: state["peak"] >= 2, timeout=2)
        release.set()
        scan.join(timeout=3)

        self.assertGreater(state["peak"], 1)
        self.assertEqual(len(result), 6)

    def test_unavailable_file_does_not_abort_remaining_download_batch(self):
        client = object.__new__(BlackboardClient)
        client._download_file = Mock(
            side_effect=FileUnavailableError("Blackboard no longer provides this file")
        )
        client.create_worker_client = Mock(return_value=client)
        client.close = Mock()
        selection = CourseSelection(
            Course("course", "Course", "CODE", "Term"),
            (
                ContentNode(
                    "content",
                    "Problems",
                    (RemoteFile("https://example.test/missing", "Problems", ".pdf"),),
                ),
            ),
        )
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            result = client.download_courses(
                [selection],
                Path(directory),
                {".pdf"},
                lambda kind, message, _current, _total: messages.append((kind, message)),
                threading.Event(),
            )
        self.assertEqual(result, (0, 0, 1))

    def test_download_file_maps_http_404_to_unavailable(self):
        response = Mock(status_code=404)
        client = object.__new__(BlackboardClient)
        client.session = Mock()
        client.session.get.return_value = response
        with self.assertRaises(FileUnavailableError):
            client._download_file(
                "https://example.test/missing",
                Path("Problems"),
                threading.Event(),
            )
        response.close.assert_called_once()

    def test_multiple_post_pdfs_survive_name_collisions_and_repeat_downloads(self):
        for names in [
            ("notes.pdf", "notes.pdf"),
            ("notes.pdf", "NOTES.pdf"),
            ("notes?.pdf", "notes*.pdf"),
            ("download", "download"),
            ("first.pdf", "second.pdf"),
        ]:
            with self.subTest(names=names), tempfile.TemporaryDirectory() as directory:
                client = object.__new__(BlackboardClient)
                client.session = Mock()

                def response_for(url, **_kwargs):
                    response = Mock(status_code=200)
                    response.headers = {
                        "Content-Disposition": 'attachment; filename="notes.pdf"'
                    }
                    response.iter_content.return_value = [url.encode()]
                    return response

                client.session.get.side_effect = response_for
                client.create_worker_client = lambda: client
                client.close = Mock()
                files = tuple(
                    RemoteFile(f"https://example.test/{index}", name, ".pdf")
                    for index, name in enumerate(names)
                )
                selection = CourseSelection(
                    Course("course", "Course", "", ""),
                    (ContentNode("post", "Documents.pdf", files + (files[0],)),),
                )
                events = []

                def download():
                    return client.download_courses(
                        [selection], Path(directory), {".pdf"},
                        lambda *event: events.append(event), threading.Event(),
                    )

                self.assertEqual(download(), (2, 0, 0))
                saved = list((Path(directory) / "Course" / "Documents.pdf").glob("*.pdf"))
                self.assertEqual(
                    {path.read_bytes() for path in saved},
                    {remote.url.encode() for remote in files},
                )
                self.assertEqual(download(), (0, 2, 0))
                self.assertEqual(
                    [event for event in events if event[0] == "file"][-1][2:], (2, 2)
                )
                selection = CourseSelection(selection.course, (
                    ContentNode("post", "Documents.pdf", tuple(reversed(files))),
                ))
                self.assertEqual(download(), (0, 2, 0))

    def test_repeated_folder_titles_are_collapsed(self):
        client = object.__new__(BlackboardClient)
        client._download_file = Mock(side_effect=lambda _url, path, _cancel: (path, True))
        client.create_worker_client = Mock(return_value=client)
        client.close = Mock()
        tree = ContentNode(
            "root",
            "Weekly Schedule",
            children=(
                ContentNode(
                    "ca1",
                    "CA1",
                    children=(
                        ContentNode(
                            "duplicate",
                            "CA1",
                            (RemoteFile("https://example.test/file", "Problems.pdf", ".pdf"),),
                        ),
                    ),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            client.download_courses(
                [CourseSelection(Course("c", "Math", "", ""), (tree,))],
                Path(directory),
                {".pdf"},
                lambda *_args: None,
                threading.Event(),
            )
        destination = client._download_file.call_args.args[1]
        self.assertEqual(sum(part == "CA1" for part in destination.parts), 1)

    def test_compact_name_preserves_extension_and_length(self):
        result = compact_name("a" * 200 + ".pdf", max_length=48, preserve_extension=True)
        self.assertLessEqual(len(result), 48)
        self.assertTrue(result.endswith(".pdf"))

    def test_file_wrapper_node_does_not_create_filename_directory(self):
        wrapped_file = RemoteFile(
            "https://example.test/chapter-3", "Chapter 3 Slides.pdf", ".pdf"
        )
        wrapper = ContentNode(
            "slides-file", "Chapter 3 Slides.pdf", (wrapped_file,)
        )
        self.assertTrue(is_file_wrapper_node(wrapper))

        tree = ContentNode(
            "materials",
            "Course Materials",
            children=(
                ContentNode(
                    "slides",
                    "Lecture Slides",
                    children=(wrapper,),
                ),
            ),
        )
        client = object.__new__(BlackboardClient)
        client._download_file = Mock(side_effect=lambda _url, path, _cancel: (path, True))
        client.create_worker_client = Mock(return_value=client)
        client.close = Mock()
        with tempfile.TemporaryDirectory() as directory:
            client.download_courses(
                [CourseSelection(Course("c", "Math", "", ""), (tree,))],
                Path(directory),
                {".pdf"},
                lambda *_args: None,
                threading.Event(),
            )
        destination = client._download_file.call_args.args[1]
        self.assertEqual(
            destination.parts[-2:],
            ("Lecture Slides", "Chapter 3 Slides.pdf"),
        )

    def test_legacy_filename_directory_is_migrated_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "Chapter 3 Slides.pdf"
            destination.mkdir()
            legacy_file = destination / destination.name
            legacy_file.write_bytes(b"slides")

            self.assertTrue(migrate_legacy_file_wrapper(destination))
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.read_bytes(), b"slides")
            self.assertFalse(any(path.name.startswith(".bb-wrapper-") for path in Path(directory).iterdir()))

    def test_downloads_run_in_parallel_and_report_completed_files(self):
        condition = threading.Condition()
        release = threading.Event()
        state = {"active": 0, "peak": 0}

        class Worker:
            def _download_remote(self, _remote, destination, _cancel):
                with condition:
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                    condition.notify_all()
                release.wait(2)
                with condition:
                    state["active"] -= 1
                return destination, True

            def close(self):
                pass

        client = object.__new__(BlackboardClient)
        client.create_worker_client = lambda: Worker()
        files = tuple(
            RemoteFile(f"https://example.test/{index}", f"file-{index}.pdf", ".pdf")
            for index in range(6)
        )
        selection = CourseSelection(
            Course("course", "Course", "CODE", "Term"),
            (ContentNode("content", "Documents", files),),
        )
        events = []
        result = []

        with tempfile.TemporaryDirectory() as directory:
            download = threading.Thread(
                target=lambda: result.extend(
                    client.download_courses(
                        [selection],
                        Path(directory),
                        {".pdf"},
                        lambda kind, message, current, total: events.append(
                            (kind, message, current, total)
                        ),
                        threading.Event(),
                        threading.Semaphore(8),
                    )
                )
            )
            download.start()
            with condition:
                condition.wait_for(lambda: state["peak"] == 4, timeout=2)
            release.set()
            download.join(timeout=3)

        self.assertEqual(state["peak"], 4)
        self.assertEqual(result, [6, 0, 0])
        file_events = [event for event in events if event[0] == "file"]
        self.assertEqual(file_events[-1][2:], (6, 6))

    def test_optional_collection_accepts_blackboard_400(self):
        client = object.__new__(BlackboardClient)
        client.base_url = "https://learn.example.edu"
        client.session = Mock()
        client.session.get.return_value = Mock(status_code=400)
        self.assertEqual(client._paged_json("https://example.test", missing_ok=True), [])

    def test_required_collection_keeps_blackboard_400_fatal(self):
        client = object.__new__(BlackboardClient)
        client.base_url = "https://learn.example.edu"
        client.session = Mock()
        client.session.get.return_value = Mock(status_code=400)
        with self.assertRaises(BlackboardError):
            client._paged_json("https://example.test")

    def test_current_user_builds_display_name(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "id": "_42_1",
            "userName": "student42",
            "name": {"given": "Ada", "family": "Lovelace"},
        }
        client = object.__new__(BlackboardClient)
        client.base_url = "https://learn.example.edu"
        client.session = Mock()
        client.session.get.return_value = response
        profile = client.get_current_user()
        self.assertEqual(profile.id, "_42_1")
        self.assertEqual(profile.display_name, "Ada Lovelace")

    def test_school_name_falls_back_to_institution_domain(self):
        client = object.__new__(BlackboardClient)
        client.base_url = "https://ntulearn.ntu.edu.sg"
        self.assertEqual(client._school_name_from_host(), "NTU")

    def test_rendered_blackboard_logo_uses_stable_partial_class_selector(self):
        element = Mock()
        element.is_displayed.return_value = True
        element.size = {"width": 120, "height": 40}
        element.get_attribute.side_effect = lambda name: (
            "Nanyang Technological University logo" if name == "aria-label" else None
        )
        element.find_elements.return_value = []
        element.screenshot_as_png = b"rendered-png"
        driver = Mock()
        driver.find_elements.return_value = [element]
        client = object.__new__(BlackboardClient)
        client.base_url = "https://ntulearn.ntu.edu.sg"
        client.driver = driver
        branding = client._capture_browser_branding()
        self.assertEqual(branding.name, "Nanyang Technological University")
        self.assertEqual(branding.logo_bytes, b"rendered-png")
        selector = driver.find_elements.call_args.args[1]
        self.assertEqual(
            selector,
            "div.MuiDrawer-root.MuiDrawer-anchorLeft header > a > img",
        )


class ContentDiscoveryRegressionTest(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(BlackboardClient)
        self.client.base_url = "https://learn.example.edu"
        self.client.session = Mock()

    def test_stub_click_target_precedes_rendering_resource_and_merges_only_same_xid(self):
        files = self.client._extract_body_files(
            """<a href="@X@EmbeddedFile.requestUrlStub@X@bbcswebdav/xid-1217_1?signature=one"
            data-bbfile='{"linkName":"notes.pdf","mimeType":"application/pdf","resourceUrl":"/bbcswebdav/xid-1217_1?signature=two"}'>notes</a>
            <a href="/bbcswebdav/xid-1217_1?signature=three">notes.pdf</a>
            <a href="/bbcswebdav/xid-9999_1">notes.pdf</a>""", None,
        )
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0].url, "https://learn.example.edu/bbcswebdav/xid-1217_1?signature=one")
        self.assertEqual(set(files[0].alternatives), {
            "https://learn.example.edu/bbcswebdav/xid-1217_1?signature=two",
            "https://learn.example.edu/bbcswebdav/xid-1217_1?signature=three",
        })
        self.assertNotEqual(files[0].identity, files[1].identity)

    def test_distinct_files_beneath_same_xythos_folder_are_not_merged(self):
        files = self.client._extract_body_files(
            '<a href="/bbcswebdav/xid-123_1/one.pdf">notes.pdf</a>'
            '<a href="/bbcswebdav/xid-123_1/two.pdf">notes.pdf</a>', None,
        )
        self.assertEqual({file.url for file in files}, {
            "https://learn.example.edu/bbcswebdav/xid-123_1/one.pdf",
            "https://learn.example.edu/bbcswebdav/xid-123_1/two.pdf",
        })

    def test_expired_blackboard_session_is_not_reported_as_missing_file(self):
        self.client.session.get.return_value = Mock(status_code=401)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "notes.pdf"
            with self.assertRaises(AuthenticationError):
                self.client._download_remote(
                    RemoteFile(self.client.base_url + "/file", "notes.pdf", ".pdf"),
                    destination, threading.Event(),
                )
            self.assertFalse(destination.exists())

    def test_malformed_metadata_and_embedded_media_keep_real_targets(self):
        files = self.client._extract_body_files(
            """<a data-bbfile="invalid" href="/notes.pdf?key=signed">Download</a>
            <img src="/figure.png"><audio src="/recording.mp3"></audio>
            <video src="/lecture.mp4"><source src="/lecture.webm"></video>
            <object data="/sheet.xlsx"></object><a href="/bbcswebdav/xid-22_1">Unknown</a>
            <a href="/content.jsp">not-a-file.pdf</a>""", None,
        )
        self.assertEqual({file.extension for file in files},
                         {".pdf", ".png", ".mp3", ".mp4", ".webm", ".xlsx", ".bin"})
        self.assertEqual(files[0].url, "https://learn.example.edu/notes.pdf?key=signed")

    def test_same_xid_alternative_recovers_in_one_download_job(self):
        remote = RemoteFile("https://learn.example.edu/bbcswebdav/xid-1_1?old",
                            "notes.pdf", ".pdf",
                            ("https://learn.example.edu/bbcswebdav/xid-1_1?new",))
        missing = Mock(status_code=404)
        available = Mock(status_code=200, headers={"Content-Type": "application/pdf"})
        available.iter_content.return_value = [b"%PDF-1.4\nRecovered notes"]
        self.client.session.get.side_effect = [missing, available]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "notes.pdf"
            self.client._download_remote(remote, destination, threading.Event())
            self.assertEqual(destination.read_bytes(), b"%PDF-1.4\nRecovered notes")
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_refresh_matches_identity_not_reused_filename(self):
        old = "https://learn.example.edu/bbcswebdav/xid-1_1?old"
        source = "https://learn.example.edu/learn/api/public/v1/courses/c/contents/i"
        remote = RemoteFile(old, "notes.pdf", ".pdf", identity=self.client._file_identity(old),
                            source_url=source)
        response = Mock(status_code=200)
        response.json.return_value = {"body": '<a href="/bbcswebdav/xid-2_1?new">notes.pdf</a><a href="/bbcswebdav/xid-1_1?new">notes.pdf</a>'}
        self.client.session.get.return_value = response
        self.assertEqual(self.client._refresh_file(remote),
                         ("https://learn.example.edu/bbcswebdav/xid-1_1?new",))

    def test_login_html_is_not_saved_as_pdf(self):
        response = Mock(status_code=200, headers={"Content-Type": "text/html"})
        response.iter_content.return_value = [b'<!doctype html><form>Sign in</form>']
        self.client.session.get.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "notes.pdf"
            with self.assertRaises(FileUnavailableError):
                self.client._download_file("https://learn.example.edu/login?secret=value",
                                           destination, threading.Event())
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".pdf.part").exists())

    def test_generated_bytes_share_filters_progress_and_do_not_flatten(self):
        remote = RemoteFile("https://learn.example.edu/content", "Notes.md", ".md",
                            content=b"# Notes\n\nA meaningful course note.")
        node = ContentNode("notes", "Notes", (remote,))
        self.assertFalse(is_file_wrapper_node(node))
        self.client.create_worker_client = lambda: self.client
        self.client.close = Mock()
        events = []
        with tempfile.TemporaryDirectory() as directory:
            result = self.client.download_courses(
                [CourseSelection(Course("c", "Course", "", ""), (node,))], Path(directory),
                {".md"}, lambda *event: events.append(event), threading.Event(),
            )
            self.assertEqual(result, (1, 0, 0))
            self.assertEqual((Path(directory) / "Course" / "Notes" / "Notes.md").read_bytes(),
                             remote.content)
        self.client.session.get.assert_not_called()
        self.assertEqual([event for event in events if event[0] == "file"][-1][2:], (1, 1))

    def test_resources_subtree_and_announcement_notes_and_files(self):
        base = self.client.base_url + "/learn/api/public/v1/courses/c"
        listings = {
            f"{base}/resources?limit=100": [{"id": "folder", "name": "Materials", "type": "Folder"}],
            f"{base}/resources/folder/children?limit=100": [
                {"id": "file", "name": "reference.pdf", "type": "File",
                 "downloadUrl": "/bbcswebdav/xid-123_1?token=provided"},
            ],
            f"{base}/announcements?limit=100": [{"id": "announcement", "title": "Reminder"}],
        }
        self.client._paged_json = Mock(side_effect=lambda url, **_kwargs: listings[url])
        response = Mock(status_code=200)
        response.json.return_value = {"body": '<p>Read this before class.</p><a href="/reading.pdf">reading.pdf</a>'}
        self.client.session.get.return_value = response
        resources, announcements = self.client._course_extras("c", threading.Event())
        self.assertEqual(resources.children[0].children[0].files[0].url,
                         "https://learn.example.edu/bbcswebdav/xid-123_1?token=provided")
        self.assertTrue({".md", ".pdf"} <= {file.extension for file in announcements.children[0].files})

    def test_optional_permission_does_not_hide_required_permission_errors(self):
        self.client.session.get.return_value = Mock(status_code=403)
        self.assertEqual(self.client._course_extras("c", threading.Event()), ())
        with self.assertRaises(BlackboardError):
            self.client._paged_json("https://learn.example.edu/required")

    def test_explicit_code_files_and_html_are_files_not_navigation(self):
        files = self.client._extract_body_files(
            """<a download="example.html" href="/download?id=1">Page source</a>
            <a href="/script.js" data-bbfile='{"linkName":"script.js"}'>Code</a>
            <a href="/data.json" data-bbfile='{"linkName":"data.json"}'>Data</a>
            <a href="/course.html">Course page</a>""", None,
        )
        self.assertEqual({file.extension for file in files}, {".html", ".js", ".json"})
        response = Mock(status_code=200, headers={"Content-Type": "text/html"},
                        url="https://learn.example.edu/download?id=1")
        response.iter_content.return_value = [b"<!doctype html><html><p>Example page</p></html>"]
        self.client.session.get.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "example.html"
            self.client._download_file(files[0].url, destination, threading.Event())
            self.assertEqual(destination.read_bytes(), b"<!doctype html><html><p>Example page</p></html>")

    def test_attachment_redirect_evidence_merges_with_body_not_same_named_other_attachment(self):
        detail = Mock(status_code=200)
        detail.json.return_value = {"title": "Materials", "body":
                                   '<a href="/bbcswebdav/xid-1_1?browser=1">notes.pdf</a>'}
        matching = Mock(status_code=302, headers={"Location": "/bbcswebdav/xid-1_1?api=1"})
        unrelated = Mock(status_code=302, headers={"Location": "/bbcswebdav/xid-2_1"})
        self.client.session.get.side_effect = [detail, matching, unrelated]
        self.client._paged_json = Mock(side_effect=[
            [{"id": "a", "fileName": "notes.pdf"}, {"id": "b", "fileName": "notes.pdf"}], [],
        ])
        result = self.client._read_content_item("c", {"id": "i"}, threading.Event())
        files = [file for file in result[2] if file.content is None]
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0].url, "https://learn.example.edu/bbcswebdav/xid-1_1?browser=1")
        self.assertIn("https://learn.example.edu/learn/api/public/v1/courses/c/contents/i/attachments/a/download",
                      files[0].alternatives)
        self.assertNotEqual(files[0].identity, files[1].identity)



if __name__ == "__main__":
    unittest.main()
