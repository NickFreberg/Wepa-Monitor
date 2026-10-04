# Security, updates and availability

How ResNet Print Ops protects its data and stays up, what has been tested, and what it can't promise.
No software is "pentest-proof". This page states what is protected, how it was checked, and the known
limits, so they can be weighed honestly.

## What there is to protect

| Asset | Why it matters |
|---|---|
| Collected printer history | Months of once-a-minute readings; the basis of every figure and report |
| Investigation records | Evidence for cases with Wepa; must be provably unaltered |
| Staff directory | Names, emails, phones and residence halls of student staff (personal information) |
| Sign-in secrets | Password hashes, the session signing key, the backup key and the GitHub token |
| The app's availability | Staff rely on it to find broken printers fast |

**Who might attack it:** someone on the internet who finds the address (password guessing, common web
attacks), someone on the campus network, a curious signed-in user trying to reach the administrator's
functions, and supply-chain risk (a vulnerable or malicious package).

**Out of scope:** Wepa's own systems (the app only reads their public status page), and Azure's
infrastructure itself.

## Confidentiality, integrity and availability

### Confidentiality: only the right people see the data

* Every page and file needs a signed-in account. The only exceptions are:
  * the sign-in page;
  * the student status pages, and only when `WEPA_PUBLIC_STATUS=1`;
  * the signed backup-collector API.

  A test walks every route in the app to confirm this.
* Sessions are signed cookies: HttpOnly, SameSite=Lax, and Secure over HTTPS. They end after 2 h idle or
  12 h in total, and at once on a password change, an administrator reset or deactivation.
* Passwords follow NIST SP 800-63B: 12+ characters, and common or breached passwords are refused (the
  breach check uses k-anonymity, so only 5 characters of a hash leave the server). They are stored as
  scrypt hashes. Lockouts apply per account and per network.
* Residence halls and sign-in times are shown only to the administrator and the person themselves.
* Secrets live in Container Apps secrets and are passed by reference. They are never in the code, the
  logs or the activity feed.
* The vulnerability check sends only package names and versions to OSV.dev. The AI assistant is sent
  computed printer figures only, never the staff directory.

### Integrity: the data and the code can be trusted

* Raw readings are append-only and never edited. Every figure is recomputed from them.
* Investigations are an event log with a SHA-256 hash chain. Changing any past event breaks the chain,
  and the self-check reports it at once (tested).
* Records are never deleted; investigations are archived after 2 years.
* Forms carry CSRF tokens, and cross-site POSTs are refused. Redirects stay on this site.
* The Content Security Policy allows scripts and styles only from the app itself. AI answers are shown
  without links, images or HTML.
* XML from outside sources is parsed with defusedxml, which refuses entity-expansion attacks.
* Every package is installed at an exact version, checked against its published SHA-256 hash
  (`requirements.lock`, `--require-hashes`), in CI and in Azure.
* Backup collectors sign every message, and the main app signs every reply (details below).

### Availability: it keeps working

* Deploys are rolling: the old copy serves until the new one is ready, and the collector hands over
  through a lock without a gap.
* Backup collectors take over within about a minute of the main collector stopping (below).
* `REPLICAS=2` in `deploy.sh` runs two copies: one collects, and the other serves pages and takes over
  collecting the moment the first stops.
* Monitoring gaps are recorded as data and shown, never filled in by guesses.
* Uploads are capped at 8 MB. Sign-in guessing is rate-limited.

## Attacker-style tests

These run on every push and every day in CI (`pytest`), next to `pip-audit` (known vulnerabilities) and
`bandit` (static analysis of the app's code).

| Attack tried | Expected result | Test |
|---|---|---|
| Open every route without signing in, with every HTTP method | Refused (or 404 for the backup API when it's off) | `test_hardening::test_every_route_needs_a_session` |
| Missing security headers | CSP, nosniff, frame and referrer policy present | `test_hardening::test_security_headers` |
| Path traversal, oversize uploads, odd input | Refused; nothing leaks | `test_hardening::test_hostile_inputs` |
| AI answer carrying links, images or script | Stripped | `test_hardening::test_ai_output_cannot_carry_links_or_markup` |
| Password guessing on the admin API | Account locks after 5 failures | `test_hardening::test_admin_api_guessing_is_locked_out` |
| Cross-site POST, forged CSRF token, open redirect | Refused | `test_site_login::test_csrf_origin_and_redirect_protection` |
| Reusing a session after a password change or deactivation | Signed out | `test_site_login::test_password_change_and_deactivation_end_other_sessions` |
| Picture upload carrying metadata or a non-image | Re-encoded without metadata, or refused | `test_site_login::test_profile_picture_is_cleaned_and_phone_normalized` |
| Unsigned, wrong-key, tampered, old or replayed backup messages | Refused | `test_software::test_signing_refuses_tampering_old_messages_and_replays`, `test_peer_api_is_off_without_a_key_and_refuses_unsigned` |
| An impostor answering as the main app, to make the backup stand down | Ignored; the backup keeps collecting | `test_software::test_forged_status_from_an_impostor_is_ignored` |
| Path tricks in backup uploads (kind, day, backup name) | Refused | `test_software::test_upload_rejects_bad_input` |
| A signed-in staff member preparing or starting an update | Refused; administrator only | `test_software::test_only_the_administrator_can_start_updates_and_needs_the_password_again` |
| Starting an update with a wrong password | Refused, and recorded in the audit log | (same test) |
| Editing the investigations file by hand | Self-check reports a broken chain | `test_software::test_selfcheck_reports_facts_and_catches_tampering` |
| A malicious or downgrading update request | Refused by the script and the workflow | `test_software::test_prepare_update_script` |

A test suite isn't an independent penetration test. Before wider use, ask BSU's information security
office for a review.

## Vulnerabilities in dependencies

* **Every day**, the collector looks up every installed package in OSV.dev, which covers the PyPI advisory
  database and GitHub security advisories. You can also check from the Software page or with
  `python -m wepa_monitor selfcheck --vulns`.
* **Each finding** shows:
  * its CVE and GHSA numbers, and its severity (from the advisory, or computed from its CVSS vector);
  * the first version that fixes it;
  * a plain-English summary, and why the package is installed;
  * links to NIST's National Vulnerability Database, the GitHub advisory and OSV.
* **In the activity feed:** new and fixed findings appear there.
* **In CI:** `pip-audit` checks the exact lock file on every push and daily, and Dependabot proposes
  weekly updates.
* **How urgent each one is:** every CVE is looked up in the sources security teams use to prioritize.
  The finding is then ranked **Act now**, **Soon** or **Routine**, with the reasons in plain English and
  a link to each source:

  | Source | What it adds | How it's read |
  |---|---|---|
  | CISA Known Exploited Vulnerabilities (KEV) | Confirmed exploitation in the wild, ransomware use, CISA's deadline | Whole catalog, daily |
  | CISA Vulnrichment (SSVC) | Exploitation none / proof-of-concept / active; automatable | Per CVE |
  | NIST National Vulnerability Database | Official CVSS score and vector, weakness (CWE) | Per CVE (`WEPA_NVD_API_KEY` speeds it up) |
  | Microsoft Security Response Center | Whether Microsoft tracks it (Azure, Azure Linux, Windows) and reports it exploited | Per CVE |
  | FIRST EPSS | Probability of exploitation in the next 30 days | Per CVE |
  | Exploit-DB | Public exploit code | Whole archive index, weekly |
  | Metasploit Framework | A ready-made attack module | Whole module index, weekly |

  **Act now** means one of these is true:
  * it's on CISA's KEV list;
  * CISA or Microsoft reports active exploitation;
  * Metasploit has a module for it.

  **Soon** means one of these is true:
  * Exploit-DB has public code for it;
  * CISA's assessment says a proof of concept exists, or the attack can be automated;
  * EPSS is 10% or more;
  * it's rated high or critical.

  Install tools are capped at Soon, because the running app doesn't use them.

  These are look-ups of public catalogs only. Nothing is ever run against anything. The catalogs are
  downloaded whole and searched here, so those sources don't learn which packages the app uses. The
  per-CVE sources see only the CVE numbers. Each source's last read and any failure are shown on the
  Software page. Turn the look-ups off with `WEPA_THREAT_INTEL=0`.
* **Install tools:** pip and setuptools come with the Python image. They're listed but kept out of update
  packages, because they aren't used by the running app.

## OWASP Top 10 (2025) checklist

The Software page includes a checklist against OWASP's current list of the ten most critical web
application risks: A01 Broken Access Control through A10 Mishandling of Exceptional Conditions. For each
risk it lists:

* what the app does about it;
* the test or file that proves each control;
* live checks of the running copy, such as sign-in being on, a signing key being set, the audit chain
  being intact, and no exploited vulnerability in a package the app runs;
* the gaps that remain.

**Met** means every control is in place. **Partial** means a known gap is stated, such as no MFA for
A07 or no email or paging alerts for A09. **Gap** means a live check failed on this copy. It is a
self-assessment against OWASP's list, not an audit.

## Feature requests to GitHub issues

Anyone signed in can send a feature request or problem report from "Suggest a feature" (under their
picture). How it works:

* Each one gets a `REQ#########` number and is kept in the records.
* It's opened as an issue in the repository, labelled `enhancement`, `bug` or `question` plus
  `from the app`.
* The person sees whether their issue is open, closed or done.

Safeguards:

* Issues are visible to anyone who can see the repository, so they carry the person's role, not their
  name, unless `WEPA_FEEDBACK_NAMES=1`.
* `@mentions` are neutralized and HTML is escaped.
* Titles and details are length-limited, and each person can send 5 a day.
* Every request goes to the audit log.
* Without GitHub access, requests wait as "Waiting to send" and are sent later.

To turn it on, create a fine-grained token for this repository only, with "Issues: read and write" and
an expiry date. Deploy with `GITHUB_ISSUES_TOKEN=<token> ./deploy/azure-containerapps/deploy.sh`.

## Updates

The app never changes or installs its own code. An update goes through the same review as any other
change:

1. **Prepare.** The Software page works out the lowest versions that fix the open vulnerabilities. The
   administrator prepares an update package, which gets an `UPD#########` reference.
2. **Start.** The administrator re-enters their password and starts it. The app asks GitHub to run
   `.github/workflows/update.yml`. Both steps go to the audit log and the activity feed.
3. **Build and test.** GitHub:
   * raises the versions;
   * regenerates the hash-pinned lock file;
   * bumps the patch version;
   * writes a plain-English change-log entry with a link to every CVE and GHSA;
   * runs lint, every test, `pip-audit` and `bandit`;
   * opens a pull request only if all of them pass.
4. **Review and deploy.** A person reviews and merges it. The deploy follows, either `deploy.sh` or
   `.github/workflows/deploy.yml` if it's turned on.
5. **Announce.** When the new version first starts, the activity feed says "Software updated to version
   X (from Y)". Clicking it opens that version's change log.

**Turning on in-app starts:** create a fine-grained GitHub token for this repository only, with the
permission "Actions: read and write" and an expiry date. Then deploy with
`GITHUB_UPDATE_TOKEN=<token> ./deploy/azure-containerapps/deploy.sh`. Without the token, run the same
workflow from GitHub's Actions tab.

**Turning on automatic deploys (optional):**

1. In Azure, create an app registration with a federated credential for this repository's `production`
   environment (OpenID Connect, so no stored password).
2. Give it the Contributor role on `rg-resnet-print-ops`.
3. In GitHub, set the secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID`, and the
   variable `AZURE_DEPLOY=true`.
4. Add required reviewers to the `production` environment so every deploy needs approval.

Pull requests opened by the update workflow don't trigger the regular CI run, which is a GitHub rule.
The workflow runs the same checks itself before it opens the pull request.

## Backup collectors

A backup is any computer that can run the app, such as a Mac in the ResNet office or a Raspberry Pi:

```bash
export WEPA_PEER_KEY='<the same 32+ character random secret the main app has>'
python -m wepa_monitor backup --primary https://<the app's address> --id office
```

Set the key on the main app with `PEER_KEY=… ./deploy/azure-containerapps/deploy.sh`. Generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

**How the handshake works**

1. **Standby.** The backup checks the main app every 15 seconds.
2. **Failover.** If the main collector has had no good reading for 60 seconds, or the app doesn't
   answer, the backup starts collecting once a minute. While the main dashboard is reachable, it sends
   each reading there, so the dashboard stays current.
3. **Handback.** At the first check that shows the main collector live again, the backup stops collecting
   at once. That happens within 15 seconds, well inside the 60-second requirement. It then reports the
   period it covered and sends everything it collected. The main app stores those readings as an import
   beside its own data, and overlapping minutes are de-duplicated.
4. **Update.** If the main app now runs a newer version, the backup fast-forwards its git checkout,
   reinstalls the pinned packages and restarts. The next failover then runs the same code. It never
   updates in the middle of a failover, and never to an older version.

**Signing**

* Every request is signed with HMAC-SHA256 over the method, path, time, a one-time number and the body's
  SHA-256.
* Requests more than 2 minutes old, or seen before, are refused.
* Replies are signed as well, and bound to the request's one-time number. An impostor answering for the
  main app can't make the backup stand down.

The Software page and the self-check show each backup's state and last check-in. Takeovers and
handbacks appear in the activity feed.

## Self-check

`python -m wepa_monitor selfcheck`, the Software page and the assistant's `self_check` tool all run the
same checks:

* collector freshness and data quality;
* the investigation hash chain and pinned dependencies;
* sign-in, sign-in attempts and process privileges;
* known vulnerabilities and the running version;
* update packages, backup collectors and disk space.

When asked about itself, the assistant answers in the first person from these results only. It doesn't
claim anything it didn't check, never calls the app absolutely secure, and is software checking itself,
not something with awareness.

## Known limits

* **No multi-factor authentication.** This was requested; the path to it is BSU's Entra ID single
  sign-on (see ACCOUNTS.md).
* **The administrator is one shared account** (`bsuresnet`). Its actions are audited, but not tied to a
  person. Use a password manager and share it with as few people as possible.
* **The container runs as root**, the default of the stock `python:3.12-slim` image. A custom image with
  an unprivileged user would reduce the damage of any future flaw; the self-check notes this.
* **Lockout and replay memory is per copy.** With `REPLICAS=2`, each copy counts sign-in failures and
  backup one-time numbers on its own. The 2-minute message lifetime still bounds replays.
* **The vulnerability check is only as current as the advisory databases**, and a flaw no one has
  reported yet can't be listed.
* **Backup self-updates trust the git remote.** Use a checkout of this repository over HTTPS.

## Reporting a problem

Email the ResNet administrator with what you found and how to reproduce it. Please don't test against
the live app without permission.
