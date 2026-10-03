# Investigations: governance

How ResNet Print Ops documents recurring printer problems, and the rules that keep the record
trustworthy enough to hand to Wepa.

## Purpose and scope

An **investigation (INV#########)** is documented root-cause work on a printer problem that keeps
happening or is clearly unusual: network or connection drop-offs (**OUT**) and hardware or system
faults (**ERR**). Paper and supply problems are routine refills and are out of scope.

It is an **evidence file, not a ticket.** BSU's ITSM system remains the system of record for support
work. If a ticket is opened there, record its number on the investigation (*ITSM ticket #*). If a case
is opened with Wepa, record it too (*Wepa case #*). Nothing is synced between systems.

## Reference numbers

Every outage gets a permanent number when the monitor records it, by its main cause:

| Prefix | Meaning |
|---|---|
| `OUT` | Unreachable or unresponsive: the printer isn't talking to Wepa |
| `JAM` | Jammed |
| `PAP` | Out of paper, or a tray disengaged |
| `SUP` | Consumable out: toner depleted or drum expired |
| `ERR` | Hardware or system: system fault, service required, cover open, or no cause reported |
| `INV` | An investigation |

Numbers have 9 digits, are never reused, and never change. Warnings (degraded periods) don't get numbers.
A gap in the monitor's own data ("No signal") isn't an outage and gets no number.

## Lifecycle

```
New ──► Analyze ──► Respond ──► Review ──► Closed Complete
 │         │           │          │  └───► Closed Incomplete
 │         │           │          └──────► back to Respond (with a reason)
 │         │           └─────────────────► Closed Incomplete
 │         ├─────────────────────────────► Closed Cancelled / Closed Incomplete
 └───────────────────────────────────────► Closed Cancelled
```

| To enter | Requires |
|---|---|
| Analyze | An assignee. Sets the **escalation date**. |
| Respond | A root cause, marked **Suspected** or **Confirmed** |
| Review | The action taken (e.g. "Opened Wepa case 55821", "Fuser replaced") |
| Any Closed state, or back to Respond | A written reason |
| Closed (from Review) | A **different person** from the one who submitted it for review |

* **Editing:** fields can change only in New, Analyze and Respond. Review and closed records are
  locked; to change a record under review, the reviewer sends it back to Respond with a reason.
* **Notes** can be added at any time except to archived records, and can't be edited or removed.
* **Who can act:** named staff accounts only. The shared administrator account and viewer accounts
  can read but not change anything, because every change must carry one person's name.

## How investigations start

* **Automatically**, as **New**, when a printer has 3 or more OUT outages, or 3 or more ERR outages, within
  14 days. The description lists the outages by reference, what the printer reported, how other
  printers compared, and the chance of that many at the campus rate. There's never more than one open
  investigation per printer and category; newer outages are linked to it. After it closes, a new one
  opens only if 3 more outages happen after the closing date.
* **By hand**, from the Investigations page, by any staff account.

An automatic investigation that isn't worth pursuing is closed as **Closed Cancelled** with a reason.
That is a decision on the record, not a deletion.

## Impact

Low, Moderate or High, calculated from the data until someone sets it by hand:

1. **Usage**: this printer's printing compared with the typical printer (toner use).
2. **Redundancy**: whether there's another working printer in the building, and the walk to the nearest
   walk-in printer.
3. **Lost printing**: busy-weighted down hours in the last 30 days compared with the campus median.

Each factor scores 1 to 3. An average under 1.5 is Low, 2.5 or more is High, and anything between is
Moderate. **With under 90 days of data, impact is Moderate by default.** Setting it by hand requires a
reason, and both are recorded. The impact at the moment of closing is saved with the closing decision.

## Evidence integrity and retention

* Every change is appended to `records/investigations.jsonl`: who, when, the old and new value of
  every field changed, and the reason. Nothing is edited in place or deleted.
* Each entry includes the SHA-256 fingerprint of the entry before it. Changing or removing any entry
  breaks the chain, and the page and the evidence packet report it. The page shows the check's result
  on every visit.
* Closed investigations move to the **Archive** two years after closing. They stay read-only,
  searchable and exportable, and are never deleted. The same applies to outage reference numbers.
* The **evidence packet** (PDF, from each investigation's page) holds the record, the linked outages with
  the printer's own messages and timestamps, the notes, the full audit trail with fingerprints, and the
  integrity check: the document to attach to a Wepa case.

## Staff accounts

Accounts are created from the command line by whoever holds the shared administrator password,
against the running app, over HTTPS:

```bash
python -m wepa_monitor users add jsmith --name "Jordan Smith" --url https://<the app's address>
python -m wepa_monitor users list    --url https://…
python -m wepa_monitor users disable jsmith --url https://…     # never deleted: the audit trail refers to them
python -m wepa_monitor users enable  jsmith --url https://…
python -m wepa_monitor users reset   jsmith --url https://…     # new password
```

The tool asks for the administrator's password, or reads `WEPA_ADMIN_PASSWORD`. It generates the new
person's password and prints it once; hand it over in person or through a password manager. Use
`--role viewer` for read-only access, and `--ask-password` to type a password instead. `--url` can
come from `WEPA_URL`. Passwords are stored only as salted hashes. Every account change made through the app is written to
the System page's audit log. (`--local` edits a copy on your own computer directly, for testing.)
