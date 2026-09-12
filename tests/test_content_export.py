import unittest
from urllib.parse import parse_qs, urlsplit

from blackboard_gui.content_export import content_source_url, export_content, resolve_content_url


BASE = "https://learn.example.edu"


class ContentExportTest(unittest.TestCase):
    def note(self, body, **fields):
        item = {"id": "_42_1", "title": "Week [1]", "body": body, **fields}
        files = export_content(BASE, "_7_1", item)
        return next(file.content.decode("utf-8") for file in files if file.extension == ".md")

    def test_note_preserves_structural_formatting_and_relative_media(self):
        note = self.note('''<h2>Study plan</h2><p>Read <strong>carefully</strong> and <em>reflect</em>.</p>
            <ol start="3"><li>Review<ul><li>First topic</li><li>Second topic</li></ul></li><li>Submit</li></ol>
            <blockquote><p>Use your own words.</p></blockquote>
            <p><code>x * y</code></p><pre>if x &lt; 2:\n    print(`value`)</pre>
            <table><tr><th>Task</th><th>Due</th></tr><tr><td>Read</td><td>Monday</td></tr></table>
            <p><img src="/images/plot.png" alt="Plot [A]"></p>
            <p><a href="/help?topic=one&amp;mode=two">Help</a></p>''')
        self.assertIn("# Week \\[1\\]", note)
        self.assertIn("## Study plan", note)
        self.assertIn("**carefully**", note)
        self.assertIn("*reflect*", note)
        self.assertIn("3. Review", note)
        self.assertIn("   - First topic", note)
        self.assertIn("4. Submit", note)
        self.assertIn("> Use your own words\\.", note)
        self.assertIn("`x * y`", note)
        self.assertIn("if x < 2:\n    print(`value`)", note)
        self.assertIn("| Task | Due |\n| --- | --- |\n| Read | Monday |", note)
        self.assertIn("![Plot \\[A\\]](<https://learn.example.edu/images/plot.png>)", note)
        self.assertIn("[Help](<https://learn.example.edu/help?topic=one&mode=two>)", note)
        self.assertIn("[Blackboard source](<", note)

    def test_notes_remove_active_content_and_keep_unsafe_link_labels(self):
        note = self.note('''<p onclick="steal()">Instructions <a href="javascript:steal()">bad link</a>
            <img src="data:image/svg+xml,evil" alt="Diagram"></p>
            <!-- secret --><script>steal()</script><style>secret</style><iframe src="https://evil.test">hidden</iframe>
            <p>&lt;script&gt;literal&lt;/script&gt; [not a link](javascript:evil)</p>''')
        self.assertIn("bad link", note)
        self.assertIn("Diagram", note)
        self.assertIn("&lt;script&gt;literal&lt;/script&gt;", note)
        self.assertIn("\\[not a link\\]\\(javascript:evil\\)", note)
        for unsafe in ("onclick", "steal", "secret", "data:image", "https://evil.test", "<script>"):
            self.assertNotIn(unsafe, note)

    def test_attachment_only_body_does_not_duplicate_download_as_note_or_shortcut(self):
        body = '<p><a data-bbfile=\'{"linkName":"lecture.pdf","mimeType":"application/pdf"}\' href="@X@EmbeddedFile.requestUrlStub@X@bbcswebdav/xid-1217_1">Lecture</a></p>'
        self.assertEqual(export_content(BASE, "course", {"id": "item", "body": body}), ())
        note = self.note("<p>Read this before class.</p>" + body)
        self.assertIn("Read this before class", note)
        self.assertIn("[Lecture](<https://learn.example.edu/bbcswebdav/xid-1217_1>)", note)

    def test_placeholder_and_signed_query_are_preserved_without_guessing_ids(self):
        value = "@X@EmbeddedFile.requestUrlStub@X@bbcswebdav/xid-1217_1?token=a%2Bb&expires=123#page=2"
        expected = BASE + "/bbcswebdav/xid-1217_1?token=a%2Bb&expires=123#page=2"
        self.assertEqual(resolve_content_url(value, BASE + "/ultra/course"), expected)
        self.assertEqual(resolve_content_url("//cdn.example.test/a.pdf?x=1", BASE), "https://cdn.example.test/a.pdf?x=1")
        self.assertEqual(resolve_content_url("/notes/%E2%94%80", BASE), BASE + "/notes/%E2%94%80")

    def test_unsafe_urls_cannot_create_shortcut_control_lines(self):
        for value in ("javascript:alert(1)", "data:text/html,hi", "file:///tmp/private",
                      "https://user:password@example.test/", "https://example.test/\r\nIconFile=evil",
                      "https://example.test/%0aIconFile=evil", "https://example.test:bad/", "https://example.test\\@evil.test"):
            with self.subTest(value=value):
                self.assertEqual(resolve_content_url(value, BASE), "")
                files = export_content(BASE, "course", {"contentHandler": {"id": "resource/x-bb-externallink", "url": value}})
                self.assertEqual(files, ())

    def test_external_and_body_links_export_exact_safe_shortcuts_with_distinct_names(self):
        files = export_content(BASE, "course", {
            "id": "item", "title": "Reference", "contentHandler": {"id": "resource/x-bb-externallink", "url": "https://example.test/start?token=a%2Bb#section"},
            "body": '<p><a href="https://one.test/">Reference</a> and <a href="https://two.test/">Reference</a><a href="/file.pdf">Download</a></p>',
        })
        shortcuts = [file for file in files if file.extension == ".url"]
        self.assertEqual({file.url for file in shortcuts}, {"https://one.test/", "https://two.test/", "https://example.test/start?token=a%2Bb#section"})
        self.assertEqual(len({file.filename.casefold() for file in shortcuts}), 3)
        for file in shortcuts:
            self.assertEqual(file.content, f"[InternetShortcut]\r\nURL={file.url}\r\n".encode())

    def test_course_link_keeps_target_metadata_and_advertised_browser_source(self):
        source = BASE + "/ultra/courses/_7_1/outline?contentId=_42_1"
        item = {"id": "_42_1", "title": "Discussion", "links": [{"rel": "alternate", "href": source}],
                "contentHandler": {"id": "resource/x-bb-courselink", "targetId": "_99_1", "targetType": "Content"}}
        files = export_content(BASE, "_7_1", item)
        note = next(file.content.decode() for file in files if file.extension == ".md")
        self.assertIn("Target type: Content", note)
        self.assertIn("Target ID: \\_99\\_1", note)
        self.assertEqual([file.url for file in files if file.extension == ".url"], [source])
        self.assertEqual(content_source_url(BASE, "_7_1", item), source)

    def test_lti_shortcut_opens_blackboard_instead_of_getting_launch_endpoint(self):
        item = {"id": "tool", "title": "Publisher", "contentHandler": {"id": "resource/x-bb-blti-link", "url": "https://publisher.test/lti/launch"}}
        files = export_content(BASE, "course", item)
        shortcut = next(file for file in files if file.extension == ".url")
        self.assertEqual(urlsplit(shortcut.url).netloc, "learn.example.edu")
        self.assertEqual(parse_qs(urlsplit(shortcut.url).query), {"course_id": ["course"], "content_id": ["tool"]})
        note = next(file.content.decode() for file in files if file.extension == ".md")
        self.assertIn("not an authenticated launch", note)
        self.assertIn("contents were not downloaded", note)
        self.assertIn("publisher", note)

    def test_math_fallback_and_malformed_bbml_retain_available_content(self):
        note = self.note('<p>Equation <img data-mathml="&lt;math&gt;&lt;mi&gt;x&lt;/mi&gt;&lt;mo&gt;=&lt;/mo&gt;&lt;mn&gt;2&lt;/mn&gt;&lt;/math&gt;">'
                         '<p><math alttext="y squared"><mi>y</mi></math><p>Still readable <b>bold')
        self.assertIn("Equation x = 2", note)
        self.assertIn("y squared", note)
        self.assertIn("Still readable **bold**", note)

    def test_explicit_html_attachments_are_not_website_shortcuts(self):
        body = '<p><a data-bbfile=\'{"linkName":"exercise.html","mimeType":"text/html"}\' href="/download/123">Exercise</a> <a download="style.css" href="/download/124">Styles</a></p>'
        self.assertEqual(export_content(BASE, "course", {"id": "files", "body": body}), ())

    def test_link_only_unknown_item_preserves_advertised_browser_target(self):
        item = {"title": "Course resource", "links": [{"rel": "alternate", "href": "/ultra/resource/one"}]}
        files = export_content(BASE, "course", item)
        self.assertEqual([file.content for file in files], [
            b"[InternetShortcut]\r\nURL=https://learn.example.edu/ultra/resource/one\r\n"
        ])

    def test_adjacent_emphasis_keeps_word_separating_whitespace(self):
        note = self.note("<p><strong>Bold </strong><em>Italic</em><b> after</b><i> </i>end</p>")
        self.assertIn("**Bold** *Italic* **after** end", note)

    def test_embedded_web_targets_become_shortcuts_not_executable_markup(self):
        files = export_content(BASE, "course", {"title": "Embedded content", "body": """
            <iframe src="/tools/view?item=1" title="Tool" onload="evil()"></iframe>
            <object data="https://examples.test/interactive" type="text/html"></object>
            <embed src="https://examples.test/player">
            <iframe src="javascript:evil()"></iframe>
            <object data="/download/report" type="application/pdf"></object>
            <embed src="/media/lecture.mp4" type="video/mp4">
            <template><iframe src="https://examples.test/hidden"></iframe></template>
        """})
        self.assertEqual({file.url for file in files}, {
            BASE + "/tools/view?item=1", "https://examples.test/interactive", "https://examples.test/player"
        })
        for file in files:
            self.assertEqual(file.extension, ".url")
            self.assertEqual(file.content, f"[InternetShortcut]\r\nURL={file.url}\r\n".encode())


if __name__ == "__main__":
    unittest.main()
