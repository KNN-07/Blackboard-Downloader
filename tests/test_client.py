import threading
import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

from blackboard_gui.client import (
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
        client._paged_json = Mock(return_value=roots)
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
        self.assertTrue(any("Unavailable on Blackboard" in message for _, message in messages))

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

    def test_alternate_url_can_recover_same_logical_file(self):
        client = object.__new__(BlackboardClient)
        client._download_file = Mock(
            side_effect=[
                FileUnavailableError("stale URL"),
                (Path("Problems.pdf"), True),
            ]
        )
        client.create_worker_client = Mock(return_value=client)
        client.close = Mock()
        files = (
            RemoteFile("https://example.test/stale", "Problems.pdf", ".pdf"),
            RemoteFile("https://example.test/current", "Problems.pdf", ".pdf"),
        )
        selection = CourseSelection(
            Course("course", "Course", "CODE", "Term"),
            (ContentNode("content", "CA1", files),),
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
        self.assertEqual(result, (1, 0, 0))
        self.assertFalse(any("Unavailable on Blackboard" in message for _, message in messages))

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
            def _download_file(self, _url, destination, _cancel):
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


if __name__ == "__main__":
    unittest.main()
