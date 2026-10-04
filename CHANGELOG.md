# Change log

Every release of BSU Student Printing Ops (called ResNet Print Ops until version 1.2.2), newest first, in plain English. Security fixes name the
vulnerability (CVE or GitHub advisory) with links to the original sources. The app shows this file on
its Change log page; click a version number in the activity feed to open that release.

Format: one `## [version] - date` heading per release, a one-line summary, then `### New`,
`### Changed`, `### Fixed`, `### Security` and `### Sources` lists. Updates prepared in the app add
their own entry here through the update pipeline (see docs/SECURITY.md).

## [1.4.0] - 2026-10-04
The background becomes a wireframe landscape, every background is easier to see, and there's one more
surprise for people who ask the right question.

### Changed
- The pattern behind the BSU, Light and Dark appearances, the sign-in page and the documentation is now a rolling wireframe terrain, like a 3D mesh, that fills the window. Printers, paper, charts, bears and Boyden Hall are drawn onto its surface in the same thin lines. Lines fade with distance, and a few vertices are marked as data points. Go Bears uses the same landscape with paw prints, footballs, goalposts and pennants.
- Every background, including the hidden themes, is about twice as strong as before: easy to notice, still behind the data.
- Secondary text and the status colors (warning, error, info) are a shade darker in light themes and a shade lighter in dark ones. All text keeps at least 4.5 to 1 contrast (WCAG 2.2 AA) even where it crosses a line of the background.

### New
- Another surprise, for the musically inclined, hidden in Ask the data. If it plays music, a "Stop the music" button stays on screen until it stops, and Escape stops it too. Operators: see wepa_monitor/sandman.py.

### Sources
- Pull request branch `claude/dazzling-volta-8b6lst`
- WCAG 2.2 success criterion 1.4.2 Audio Control: https://www.w3.org/TR/WCAG22/#audio-control

## [1.3.0] - 2026-10-04
Boyden Hall becomes a true 3D wireframe, and on football game days the app cheers for the Bears.

### New
- Go Bears: on any day the Bridgewater State football team plays, home or away, the BSU appearance becomes Go Bears. It has a crimson and gold sidebar, gold highlights, a background of paw prints, footballs, goalposts and pennants, and a banner with the opponent, kickoff time and any special occasion (Homecoming, the Cranberry Bowl). The schedule comes from the public calendar feed on bsubears.com, refreshed every 12 hours, with this season's games stored in reference/football.csv in case the feed can't be reached.
- There may be a surprise or two in Appearance for people who keep tapping. Each one can be undone with "Bring back the original themes".

### Changed
- The Boyden Hall logo is now a 3D wireframe model in a three-quarter view: the wings, portico and pediment, and the tower with its clock, belfry and faceted dome. Back edges show through, small squares mark the corners, and the building stands on a grid floor. The sidebar mark, browser icon, sign-in page and documentation cover all use it.

### Sources
- Pull request branch `claude/dazzling-volta-8b6lst`
- Bridgewater State football schedule: https://bsubears.com/sports/fball/2026-27/schedule

## [1.2.3] - 2026-10-04
A new look: Boyden Hall, drawn as a wireframe, is the app's logo, and a quiet pattern of data, charts,
printers and a few bears sits behind every page.

### Changed
- The logo is Boyden Hall drawn in thin lines, with offset crimson, gold and blue copies that echo the lines in data charts. It replaces the BSU bear, which is a university trademark. The browser tab icon matches.
- A geometric background (a network mesh with small printers, paper, bar charts, sparklines, donuts, scatter plots, bears and Boyden Hall) sits behind the cards on every page, the sign-in page and the documentation cover. Its colors follow the chosen theme.
- The background is hidden in high-contrast mode, when the system asks for more contrast, in Windows forced colors and in print.

### Fixed
- Secondary (muted) text now meets a contrast of at least 5 to 1 in every theme, even over the darkest part of the pattern. In the light theme it was 3.5 to 1 before, below the WCAG AA minimum of 4.5 to 1.

### Sources
- Pull request branch `claude/dazzling-volta-8b6lst`
- WCAG 2.2 success criteria 1.4.3 Contrast (Minimum) and 1.4.11 Non-text Contrast: https://www.w3.org/TR/WCAG22/

## [1.2.2] - 2026-10-04
ResNet Print Ops is now BSU Student Printing Ops, a name that covers every station it watches, from the
residence halls to the labs, the library and the satellite campus. East Campus Commons now counts as a
printer every student can walk into.

### Changed
- New name everywhere people see it: the app, the sign-in page, exports, evidence packets, feature requests, the assistant and the documentation. The Azure app keeps its address.
- East Campus Commons is offered as a nearby printer when another one is down. It is run by ResNet, but its dining hall, Dunkin', bookstore and ResNet office make it open to every student. A new access column in the station list records who can walk up to each printer.

### New
- Product documentation (docs/product/): the full CRISP-DM lifecycle, requirements, data dictionary, architecture, process models, user guide and standards alignment, as a web page and a PDF.

### Sources
- Pull request branch `claude/dazzling-volta-8b6lst`

## [1.2.1] - 2026-10-04
Times read in whole units, and the layout fits any window.

### Changed
- Every length of time is written in whole units, never decimals: "3 hours, 15 minutes", and past a day "2 days, 4 hours, 10 minutes". This covers tiles, tables, stories, chart labels and hovers, the activity feed, investigations and the assistant's answers. Totals across printers read as printer time ("75 days, 16 hours of printer downtime") instead of printer-hours.
- The sidebar's width follows the window. It becomes an icon rail on tablet-size windows and a strip along the top on phones, and its color runs the full length of every page. On short windows the navigation tightens, and it scrolls if it still doesn't fit.
- The top bar's margins match the page content, button labels give way to icons before anything wraps, and on very wide screens its tools line up with the content's right edge.

### Fixed
- Long values in the summary tiles no longer push the page wider than the window; they wrap at the commas.
- Chart labels for long durations are no longer cut off.

## [1.2.0] - 2026-10-04
Vulnerabilities are now ranked by real-world exploitation, the app checks itself against the OWASP Top 10,
and anyone signed in can suggest a feature that goes straight to GitHub as an issue.

### New
- Threat intelligence for every vulnerability found: CISA's Known Exploited Vulnerabilities catalog and Vulnrichment assessments, the NIST National Vulnerability Database, Microsoft's Security Update Guide, FIRST EPSS, Exploit-DB and Metasploit. Each finding gets a priority (Act now, Soon or Routine) with the reasons in plain English and links to every source.
- A Threat-intelligence sources table showing when each source was last read and whether it answered.
- OWASP Top 10 (2025) checklist: for each of the ten risks, what the app does, the test or file that proves it, a live check of this copy, and the known gaps.
- Suggest a feature: a form under your picture that files a GitHub issue with a REQ reference number, and a list of your requests showing whether each issue is open, closed or done. Names are left out of issues unless turned on; @mentions are neutralized.
- New activity-feed events when a vulnerability becomes exploited and when a feature request is sent.

### Changed
- "Check for vulnerabilities now" runs in the background; the page updates when it finishes.

### Sources
- Pull request branch `claude/dazzling-volta-8b6lst`

## [1.1.0] - 2026-10-04
The app now watches its own health and security: known vulnerabilities in its packages, software
updates you can start from the app, backup collectors that cover outages and hand back cleanly, and a
self-check the assistant can explain.

### New
- Software page: the running version, what changed in it, every known vulnerability in the installed packages, and a self-check of the whole app.
- Daily vulnerability check of every installed package against OSV.dev (PyPI and GitHub advisories). Each finding shows its CVE or GHSA number, severity, the version that fixes it, a plain-English summary and links to the sources.
- Update packages: the app works out which package versions fix the open vulnerabilities, and the administrator can start the update from the app. GitHub then makes the change, runs every test and security scan, and opens a pull request; nothing is installed until it passes and is approved.
- Change log pages. Software updates appear in the activity feed; click the version to read what changed.
- Backup collectors: a second computer can stand by, take over collecting within about a minute of the main collector stopping, keep the dashboard current, and step down within a minute of the main collector returning. Every message between them is signed. After handing back it sends what it collected and updates itself to the main app's version.
- Self-check: collector freshness, data quality, the investigation audit trail, vulnerabilities, version, backups, configuration and disk, in one list. The assistant can run it and answer questions about the app itself.
- Security documentation (docs/SECURITY.md): threat model, how confidentiality, integrity and availability are protected, attacker-style test results, and known limits.

### Sources
- Pull request branch `claude/dazzling-volta-8b6lst`

## [1.0.0] - 2026-10-03
Security hardening ahead of wider use.

### Security
- Content Security Policy on every page; scripts and styles only from the app itself, maps only from named tile servers.
- Exact, hash-checked package versions (requirements.lock) for every install, in testing and in Azure.
- Every push and every day: a known-vulnerability scan of all packages (pip-audit with OSV) and static analysis of the app's code (bandit).
- XML from outside sources is parsed with defusedxml, which refuses entity-expansion attacks.
- AI answers are shown without links, images or HTML, so a manipulated answer can't send people elsewhere.
- Attacker-style tests: signed-out access to every page and file, path tricks, oversize uploads, cross-site form posts and password guessing.

### Sources
- [commit 6b54580](https://github.com/nickfreberg/wepa-monitor/commit/6b54580)

## [0.8.0] - 2026-10-03
Investigations, permanent reference numbers and named staff accounts.

### New
- Investigations (INV numbers): root-cause records for recurring hardware problems, with a governed workflow, impact rating, tamper-evident history and an evidence PDF for Wepa.
- Permanent reference numbers for every outage, jam, paper and supply event (OUT, JAM, PAP, SUP, ERR).
- Staff directory with a sign-in page, secure sessions, profile pictures and phone numbers, managed by the administrator from the command line.
- Lifecycle metrics: time in each investigation state, send-backs, reassignments and time spent.

### Sources
- [commit 8b1bd13](https://github.com/nickfreberg/wepa-monitor/commit/8b1bd13)
- [commit b75a014](https://github.com/nickfreberg/wepa-monitor/commit/b75a014)
- [commit e2eb598](https://github.com/nickfreberg/wepa-monitor/commit/e2eb598)

## [0.7.0] - 2026-10-03
New analyses, student status pages and an executive status vocabulary.

### New
- Shared outages: several printers going down together, told apart from busy moments.
- Student status pages with printable QR signs.
- Busy-weighted downtime and supply reorder points.
- A test of whether humid weather goes with more paper jams.
- A case study and refreshed screenshots.

### Changed
- Status words: Operational, Degraded, Out of service, No signal; Ongoing and Resolved; "Support unavailable until …" when the help desk is closed.

### Sources
- [commit 324d114](https://github.com/nickfreberg/wepa-monitor/commit/324d114)
- [commit b9ed73b](https://github.com/nickfreberg/wepa-monitor/commit/b9ed73b)
- [commit 4a18f27](https://github.com/nickfreberg/wepa-monitor/commit/4a18f27)
- [commit e12899e](https://github.com/nickfreberg/wepa-monitor/commit/e12899e)
- [commit e36a147](https://github.com/nickfreberg/wepa-monitor/commit/e36a147)

## [0.6.0] - 2026-10-03
The AI analyst and the outage-risk model.

### New
- Claude as an AI provider, and an Assistant pane on every page.
- The assistant works as an analyst: read-only tools over all the data, an expert brief, and a fact check of every number it writes.
- An outage-risk machine-learning model that is only shown when it beats a simple baseline.
- Continuous integration: lint and the full test suite on every push.

### Sources
- [commit be5dbfe](https://github.com/nickfreberg/wepa-monitor/commit/be5dbfe)
- [commit 60af0d7](https://github.com/nickfreberg/wepa-monitor/commit/60af0d7)
- [commit e9ab685](https://github.com/nickfreberg/wepa-monitor/commit/e9ab685)
- [commit 05b46d4](https://github.com/nickfreberg/wepa-monitor/commit/05b46d4)
- [commit a25a893](https://github.com/nickfreberg/wepa-monitor/commit/a25a893)

## [0.5.0] - 2026-10-03
New header, System and IT Outcomes pages, deeper analytics and AI summaries.

### New
- Appearance menu, data export, the System page and the IT Outcomes report.
- Availability through the day, what cost the most printing time, usage and report-card tabs.
- AI-written summaries (GitHub Copilot or any OpenAI-compatible service).
- Planning and deeper statistics, and class schedules from BSU's course search.

### Changed
- Clearer fault types; outages told as stories; backups measured in miles; no dollar costs.

### Sources
- [commit 77bbe72](https://github.com/nickfreberg/wepa-monitor/commit/77bbe72)
- [commit 4a4d8a5](https://github.com/nickfreberg/wepa-monitor/commit/4a4d8a5)
- [commit 209ba85](https://github.com/nickfreberg/wepa-monitor/commit/209ba85)
- [commit 9c6dae7](https://github.com/nickfreberg/wepa-monitor/commit/9c6dae7)
- [commit bf2bc01](https://github.com/nickfreberg/wepa-monitor/commit/bf2bc01)

## [0.4.0] - 2026-10-02
Running in Azure.

### New
- Azure deployment: App Service, a virtual machine, and Azure Container Apps (the one in use).
- An optional site login.
- Importing data collected on another computer.
- Zero-downtime deploys.

### Fixed
- A crash in the first hour of data.

### Sources
- [commit 4ad2ad9](https://github.com/nickfreberg/wepa-monitor/commit/4ad2ad9)
- [commit f569e12](https://github.com/nickfreberg/wepa-monitor/commit/f569e12)
- [commit 2bf8a9f](https://github.com/nickfreberg/wepa-monitor/commit/2bf8a9f)
- [commit e9e44ff](https://github.com/nickfreberg/wepa-monitor/commit/e9e44ff)

## [0.3.0] - 2026-10-02
Plain-language stories, campus context and route planning.

### New
- Plain-language stories, "Ask the data", click-to-explain charts and support desk hours.
- Campus context from bridgew.edu: the academic calendar, residence halls and library hours.
- Rounds: a route planner for printer visits, on foot or by transit van.

### Sources
- [commit b25af8f](https://github.com/nickfreberg/wepa-monitor/commit/b25af8f)
- [commit cfdf818](https://github.com/nickfreberg/wepa-monitor/commit/cfdf818)
- [commit 2d97787](https://github.com/nickfreberg/wepa-monitor/commit/2d97787)

## [0.2.0] - 2026-10-01
A production server, the dashboard's structure and forecasts.

### New
- Day rollups and a production web server.
- Sidebar navigation, drill-through to each station, the activity feed and the BSU theme.
- Forecasts and statistical models.

### Sources
- [commit d5c73fc](https://github.com/nickfreberg/wepa-monitor/commit/d5c73fc)
- [commit 76242d4](https://github.com/nickfreberg/wepa-monitor/commit/76242d4)
- [commit 684fe5c](https://github.com/nickfreberg/wepa-monitor/commit/684fe5c)

## [0.1.0] - 2026-10-01
The monitor rebuilt: a once-a-minute snapshot of Wepa's status page and a web dashboard.

### New
- A snapshot pipeline and web dashboard.
- A campus map with building locations and a Google Earth export.
- One command to collect and view, with macOS setup instructions.

### Sources
- [commit 49af3c7](https://github.com/nickfreberg/wepa-monitor/commit/49af3c7)
- [commit 026d285](https://github.com/nickfreberg/wepa-monitor/commit/026d285)
- [commit dd74bcc](https://github.com/nickfreberg/wepa-monitor/commit/dd74bcc)
