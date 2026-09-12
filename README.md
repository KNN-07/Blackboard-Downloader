# Blackboard Downloader

A small cross-platform desktop app for saving an organized offline copy of your Blackboard Learn course files.

![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-2f6545)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![Blackboard Downloader desktop interface](docs/blackboard-downloader-demo.png)

## Why this exists

Downloading an entire Blackboard course manually is slow and error-prone. Blackboard Downloader signs in through your school's real login page, discovers the files available to your account, and saves only the course sections and file types you choose.

It is designed for Blackboard Ultra installations that expose Blackboard's public REST API.

## Highlights

- Uses the school's normal browser-based sign-in flow, including SSO and MFA.
- Supports Chrome, Microsoft Edge, and Firefox through Selenium Manager.
- Restores sign-in securely across launches using the operating system credential vault.
- Shows the signed-in account and institution branding captured from Blackboard.
- Loads course content lazily when a course is opened instead of scanning everything at startup.
- Expands courses into a selectable content tree for partial-course downloads.
- Discovers file extensions dynamically from the selected content.
- Distinguishes navigation links from explicitly attached files, including HTML, JavaScript, JSON, and other course/code files.
- Indexes courses and content items concurrently with a shared request limit.
- Downloads up to four files in parallel with file-level progress.
- Preserves useful course and content folders while removing duplicate filename directories.
- Skips existing files and safely removes incomplete `.part` files after cancellation.
- Combines proven alternate links to the same Blackboard resource without merging different files that happen to share a filename.
- Handles Windows path-length constraints and safely migrates the older `file.ext/file.ext` layout.
- Checks GitHub Releases in the background and supports opt-in verified automatic updates.
- Exports available course notes and announcements as Markdown, and web/tool references as URL shortcuts.
- Discovers readable course-resource folders and files using their advertised download URLs.

## Requirements

- Python 3.10 or newer
- Windows 10/11, macOS, or a Linux desktop
- Google Chrome, Microsoft Edge, or Mozilla Firefox
- A Blackboard account with access to the desired courses
- Linux only: a working Secret Service provider for secure session storage

## Download installers

Prebuilt packages are published on the [GitHub Releases page](https://github.com/KNN-07/Blackboard-Downloader/releases):

- Windows x64 setup installer and portable `.exe`
- macOS Apple Silicon and Intel `.dmg` images
- Linux x64 Debian package and portable `.tar.gz`

The packages are currently unsigned. Windows SmartScreen and macOS Gatekeeper may therefore ask for confirmation before the first launch. Review the source and release workflow before bypassing an operating-system warning.

## Quick start

Clone the repository and enter the project directory:

```bash
git clone https://github.com/KNN-07/Blackboard-Downloader.git
cd Blackboard-Downloader
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or on macOS and Linux:

```bash
source .venv/bin/activate
```

Install the dependencies and start the app:

```bash
python -m pip install -r requirements.txt
python main.py
```

The app also audits its Python environment at startup and provides the exact installation command if a required package is missing.

## Usage

1. Enter your Blackboard address. A full Ultra URL or just the school hostname is accepted.
2. Choose whether the sign-in should be remembered on the device.
3. Select **Open sign-in**, finish signing in through the browser, then select **I'm signed in**.
4. Expand a course to load its content. Only courses you open or select are indexed.
5. Select entire courses or individual content branches.
6. Choose from the discovered file types. Select **Markdown** for notes and **URL links** for shortcuts; previously saved file-type preferences remain in effect.
7. Select a destination and choose **Download selected**.

The progress bar tracks logical files and generated exports within each destination folder. Repeated links with the same proven Blackboard resource identity are combined; different files with matching names remain separate. Filename collisions use a stable identity suffix (for example, `Problems~13ca1b6ec28e.pdf`) so changing discovery order does not swap saved files. Catalog filenames are retained rather than overwritten by response headers. Downloaded, already-existing, and inaccessible files each advance progress once.

## Application updates

Open **Updates → Check for updates** to check manually. **Check automatically** is enabled by default: it checks shortly after launch and once every 24 hours while the app remains open. Only newer stable releases are offered; prereleases and downgrades are not installed.

**Install available update** downloads and verifies the matching package. Enable **Install automatically when idle** to opt into downloading and installing future updates without another confirmation. Installation waits for sign-in, course indexing, and course downloads to finish. The app closes for installation and restarts afterward; operating-system authorization may still be required. Both preferences are saved locally.

Supported automatic installation paths:

| Installation | Update behavior |
| --- | --- |
| Windows x64 setup | Runs the new Inno Setup installer against the existing writable installation directory. |
| Windows x64 portable | Replaces the executable after the running process exits. |
| macOS Apple Silicon / Intel | Replaces a writable installed `.app` bundle. Copy it out of the disk image first. |
| Linux x64 portable | Replaces the extracted executable in its writable directory. |
| Debian package | Uses `pkexec` and `dpkg` to update `/usr/bin/blackboard-downloader`; authorization is required. |
| Source checkout, unsupported architecture, or protected installation | Offers the release page without changing the checkout or installation. |

Downloads use a separate HTTPS connection to this project's GitHub Releases, never your Blackboard session. Before installation, the updater verifies size and SHA-256 against GitHub's asset digest or the release's `SHA256SUMS`, then checks the staged file again. These integrity checks do not replace publisher code signing; the packages remain unsigned.

The updater retains a uniquely named `.backup-*` executable or app bundle beside the installation. After confirming the new version works, you may remove that backup. Failed replacements attempt to restore and restart the old application. Post-exit installation details are written to `install.log` inside the system temporary directory's `blackboard-update-*` folder. A failed Debian transaction may still require `sudo apt --fix-broken install` to repair package-manager state.

Existing releases without this updater need one manual installation of an updater-enabled release. Subsequent release builds embed their tag version and publish `SHA256SUMS` automatically.

## Session security and privacy

Blackboard Downloader never asks for or stores your school password. Authentication happens in the school's own browser page.

When **Remember sign-in on this device** is enabled:

- Blackboard session cookies are encrypted before being written to application data.
- The encryption key is stored through `keyring` in Windows Credential Manager, macOS Keychain, or Linux Secret Service.
- Selecting **Forget sign-in** removes the saved session and its credential-vault key.

Disable the option to keep the session only for the current app run. Blackboard may expire a saved session at any time, in which case the app asks you to sign in again.

## How downloads are organized

Files are saved beneath the chosen destination using the useful parts of Blackboard's hierarchy:

```text
Blackboard/
└── Course name/
    └── Course Materials/
        └── Lecture Slides/
            └── Chapter 3 Slides.pdf
```

Content nodes that merely wrap one file are flattened, so the app does not create paths such as `Chapter 3 Slides.pdf/Chapter 3 Slides.pdf`. Repeated adjacent folder titles are collapsed, and long components receive a short stable hash to avoid collisions.

Notes and multi-file posts keep their own folders. Additional readable files appear under **Course resources**, and announcement notes appear under **Announcements**. Markdown preserves available text, headings, emphasis, lists, tables, code, and safe links/images. Math uses available alternative text or a plain-text MathML fallback; this is not an exact reconstruction of Blackboard's visual layout. Links in Markdown remain online references and may require sign-in; they are not rewritten to local attachment paths.

`.url` exports are UTF-8 InternetShortcut files containing the target URL. External resources are preserved as links, not recursively crawled. LTI, discussion, assessment, and other opaque tool items retain available metadata and a Blackboard source link; their interactive contents are not scraped or launched.

Existing local files are skipped. Older numeric collision filenames are not automatically renamed or deleted because the app cannot safely infer which remote file they contain.

## Concurrency model

The app is intentionally bounded to avoid overwhelming a school's Blackboard server:

- Up to four courses can be indexed concurrently.
- Independent content items inside a course are scanned concurrently.
- Indexing is capped at eight Blackboard requests globally.
- Up to four unique files are downloaded concurrently.
- Indexing and downloading share the same global request ceiling.

## Blackboard compatibility

The crawler was checked against the [current Learn API catalog](https://developer.blackboard.com/portal/displayApi/Learn), whose linked [Swagger specification](https://devportal-docstore.s3.amazonaws.com/learn-swagger.json) was version **4000.21.0** at review time (198 paths / 338 operations). The course-content subset is used; this is not an implementation of every Blackboard API.

| Documented surface | Use in this app |
| --- | --- |
| `/courses/{courseId}/contents`, `/{contentId}`, and `/{contentId}/children` | Traverse accessible folders/documents, read available body/description and handler metadata, and honor pagination. Detail/child reads request `includeInActivityTracking=false`. |
| `/courses/{courseId}/contents/{contentId}/attachments` and `/{attachmentId}/download` | Discover actual attachment IDs and follow returned download redirects. Upload IDs are not treated as attachment IDs. |
| Ultra document BBML | Follow the advertised browser `href`, resolve `@X@EmbeddedFile.requestUrlStub@X@`, and preserve signed query parameters. |
| `/courses/{courseId}/resources`, `/{resourceId}`, and `/{resourceId}/children` | Discover readable resource files/folders and use returned `downloadUrl` values. |
| `/courses/{courseId}/announcements` and `/{announcementId}` | Export available announcement bodies and discover their linked files. |
| Content handlers and `links` with `rel=alternate` | Export external links, Blackboard source links, and exposed metadata without fabricating hidden tool content. |

The implementation follows Blackboard's [attachment cookbook](https://docs.blackboard.com/docs/blackboard/rest-apis/demo-code/curl-attach-demo), [BBML specification](https://docs.blackboard.com/docs/blackboard/rest-apis/advanced/bbml), and [content-handler documentation](https://docs.blackboard.com/docs/blackboard/rest-apis/advanced/content-handler). Ultra document attachments can live in the BBML rather than the Original-course attachment API.

The app uses the user's captured browser session. Blackboard's [official REST integration model](https://docs.blackboard.com/docs/blackboard/rest-apis/getting-started/first-steps) uses registered OAuth applications and entitlements; browser-session access is deployment-dependent and does not grant extra permissions. Unsupported or forbidden optional collections are omitted without aborting otherwise accessible course content. Global Content Collection storage, grades, submissions, private messages, and full external-tool content are not crawled.

A failed download link does **not** prove the file was deleted. The app tries known alternatives and can refresh an owning content/resource record when a stable identity allows matching the same file. Unrelated same-named files are never substituted. A remaining failure reports its HTTP status/path without signed query parameters and continues the batch; an expired Blackboard session requires signing in again. HTML sign-in pages are rejected instead of being saved as PDFs.

If a file still opens in Blackboard but fails in the app, reconnect and re-index the course to obtain current links. Some content is not exposed by the documented APIs, and opaque Enhanced Ultra documents or tools can only be preserved using their available source link; no private endpoint is guessed.

## Project structure

```text
blackboard_gui/
├── app.py              # Tkinter interface and background task coordination
├── client.py           # Blackboard API traversal, filtering, and downloads
├── content_export.py   # Safe BBML-to-Markdown and URL shortcut exports
├── dependencies.py     # Runtime dependency audit
├── secure_store.py     # Encrypted cross-platform session persistence
└── branding_cache.py   # Institution name and logo cache
tests/                  # Unit and regression tests
assets/                 # Shared app icons for the UI and native packages
main.py                 # Application entry point
requirements.txt        # Runtime dependencies
```

## Testing

Run the test suite from the repository root:

```bash
python -m unittest discover -s tests -v
```

The regression suite covers session storage, dependency auditing, Blackboard response handling, content-tree filtering, lazy and parallel indexing, download progress, BBML URL normalization, attachment identity and stale-link recovery, readable resource/announcement discovery, Markdown/URL exports, updater safety, and legacy path migration.

## Automated builds and releases

Every push and pull request to `main` runs the test suite on Windows, macOS, and Linux with Python 3.10 and 3.12.

The **Build installers** workflow supports two release paths:

- Run it manually from the GitHub Actions page to produce downloadable workflow artifacts retained for 14 days.
- Push a version tag to build every platform and publish the results as a GitHub Release:

```bash
git tag v0.2.1
git push origin v0.2.1
```

Version tags must begin with `v` and use a value such as `v0.2.1` or `v0.2.1-beta.1`.

The release matrix produces:

```text
Windows x64       Setup.exe + portable.exe
macOS arm64       DMG
macOS x86_64      DMG
Linux x86_64      DEB + portable tar.gz
```

## Building locally

Install PyInstaller and build on the operating system you want to target:

```bash
python -m pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name "Blackboard Downloader" main.py
```

The executable is written to `dist/`. PyInstaller does not cross-compile, so Windows, macOS, and Linux builds must be produced on their respective platforms.

## Troubleshooting

### No browser opens

Update Chrome, Edge, or Firefox and confirm it can be launched normally. Selenium Manager automatically resolves a compatible browser driver.

### A saved sign-in cannot be restored

The Blackboard session may have expired, or the operating system credential vault may be unavailable. Sign in again, or disable **Remember sign-in on this device** for a one-time session.

### A course cannot be inspected

The institution may restrict the required REST endpoint or a Blackboard content handler may reject API access. Other courses remain usable.

### A file is unavailable

Blackboard sometimes leaves stale file references in course content. The app tries alternate references first, logs the unavailable file, and continues the batch.

### A destination path is too long

Choose a shorter download destination. The app already compacts generated path components, but it cannot shorten a deeply nested destination chosen outside the app.

## Contributing

Issues and focused pull requests are welcome. Please include the operating system, Python version, Blackboard interface type, reproducible steps, and relevant Activity output with personal information removed.

## Disclaimer

This is an unofficial community project and is not affiliated with or endorsed by Anthology Inc., Blackboard, Nanyang Technological University, or any other institution. Use it only with accounts and course materials you are authorized to access, and follow your institution's policies and applicable copyright rules.

## License

Blackboard Downloader is released under the [MIT License](LICENSE).
