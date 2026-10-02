# Campus intelligence: what bridgew.edu adds

A printer's numbers only mean something next to what campus was doing at the time. A 90% week
during finals is a crisis; the same 90% in July, with the residence halls empty, barely matters.
This page records what was collected from Bridgewater State's public website, how it's used, and
what was looked at but left out. Collected October 2026.

The crawl followed `robots.txt` (only `/admin`, `/user`, `/core` and similar paths are disallowed),
started from `sitemap.xml` (about 3,000 pages), fetched about a dozen relevant pages one per second,
and identifies itself with the monitor's user agent.

## Used

| Source | What it gives | How often | Where it shows up |
|---|---|---|---|
| [Academic calendar](https://www.bridgew.edu/office/registrar/academic-calendar) (Registrar) | 166 dated entries for 2025-26 to 2028-29: first and last day of classes, holidays, Thanksgiving, spring break, reading days, finals, commencement, summer sessions | Yearly (published four years ahead) | Phase of every day, holiday desk closures, story context, "Coming up", calendar bands on charts, Ask |
| [Move-in / move-out](https://www.bridgew.edu/student-life/residence-life-housing/moving-in-moving-out-process) (Residence Life) | Move-in days; when the halls close and reopen for winter, spring and summer break | Yearly (current year only) | Halls open/closed, which drives exposure and demo demand |
| [Residence halls](https://www.bridgew.edu/student-life/residence-life-housing/residence-halls) (Residence Life) | Residents per hall (e.g. Shea/Durgin 658 first-years, Weygand 500) and class standing | Yearly | Residents-per-printer chart, station pages, Ask |
| [Maxwell Library hours](https://bridgew.libcal.com/hours) (LibCal) | Opening hours by date, about 16 weeks ahead | Weekly, accumulated so past days keep their real hours | Exposure for the three library printers, overview line, Ask |
| [Student Technology](https://www.bridgew.edu/student-life/technology) | Confirms the desk hours and locations: IT Service Desk, Maxwell Library ground floor, Mon-Fri 9-4; ResNet, East Campus Commons Rm 107, Mon-Thu 10-6, Fri 10-4. Notes that after-hours phone support is outsourced 24/7 | Reference | `config.SUPPORT_TEAMS` |

### What it changes

- **Desk closures.** University holidays from the calendar, plus Massachusetts state holidays the
  calendar doesn't list because they fall in a break (e.g. MLK Day), close both desks. Outages on
  those days count as after hours. The library's own hours confirm it closes on those days
  (Oct 12, Nov 11, Nov 26). Turn this off with `DESKS_CLOSED_ON_HOLIDAYS = False`.
- **Exposure.** Each outage records how much of its downtime fell while the building was in use.
  Residence-hall printers count only while the halls are open. Library printers count only during
  library hours. A printer that dies the day the halls close for winter break shows as an outage,
  and the stories say how much of the downtime nobody was around to feel.
- **Like-for-like comparisons.** The *Across the academic year* card compares outages per day by
  phase (classes, finals, move-in, breaks, summer).
- **Stories and look-ahead.** The narrative names the point in the year ("It was finals, the
  heaviest printing of the term") and the holidays in the period. It flags the next event worth
  preparing for, with the parts projected to run out around then.
- **Demo data.** The demo follows the real calendar: busy at finals, quiet on breaks, and hall
  printers nearly idle while the halls are closed.

### Assumptions to confirm

- Residence Life lists no resident counts for **Miles** and **DiNardo**. They share the rest of
  the page's "approximately 3,300" total equally (200 each), because the page calls them mirror
  images. They're marked as estimates wherever shown.
- Hall dates for years Residence Life hasn't published yet are **inferred** with this year's
  offsets: close the day after finals, reopen 3 days before classes, close the Friday before
  spring break and reopen the Sunday after, move in 4 days before fall classes. Those offsets
  match all six 2026-27 published dates exactly.
- The move-in page lists "Friday, Dec. 19, 2026", but Dec 19, 2026 is a Saturday. The parser
  uses the date, not the weekday.
- Massachusetts state holidays are assumed to close the support desks. Add or remove days in
  `SUPPORT_CLOSED_DATES`.

## Considered, not used (yet)

| Source | Why it could matter | Why it's not in |
|---|---|---|
| [BSU Facts](https://www.bridgew.edu/about-us/bsu-facts) and the Factbook | Enrollment (9,492 in Spring 2025: 7,949 undergraduate, 1,543 graduate). Useful to normalize campus-wide usage per student | Annual and campus-wide; nothing to join printer data to below the campus level |
| [Final exam schedule](https://www.bridgew.edu/office/registrar/final-exam-schedule) | Exam blocks by day and time slot, which could predict printing spikes by hour during finals week | Finals-week granularity is enough for now; worth adding once there's live finals data to check against |
| [Inclement weather closings](https://www.bridgew.edu/inclement-weather-campus-closing-delayed-opening) | Snow days close the university and both desks | Closures are announced on BridgeNet (login required) with no public history; add them to `SUPPORT_CLOSED_DATES` when they happen |
| Kickoff and dining hours | Building activity at the start of term | One-off, and dining halls don't host printers |
| [Wepa page](https://www.bridgew.edu/student-life/technology/wepa) | Lists the public kiosk locations | Already covered by the Wepa status page itself |

Refresh everything due with `python -m wepa_monitor campus` (add `--force` to refetch all). The
collector also runs this once a day and only fetches what's due. If a page changes layout, the
parser raises an error and the previous file stays in place.
