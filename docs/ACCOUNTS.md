# Accounts and sign-in

ResNet Print Ops keeps one central staff directory, managed by the administrator, and signs people in
following NIST SP 800-63B practices (multi-factor authentication aside).

## The directory

| Field | Set by | Notes |
|---|---|---|
| Username | Administrator | 2-32 lowercase characters; permanent |
| Name, email | Administrator | Name appears on every change the person makes; emails are unique |
| Role | Administrator | `staff` (works investigations) or `viewer` (reads only) |
| Active | Administrator | Deactivating signs the person out everywhere at once; accounts are never deleted, because the audit trail refers to them |
| Resident student, home residence hall | Administrator | Yes/No; a resident needs a hall from BSU's list (`users halls`) |
| Profile picture | The person | PNG/JPEG/WebP/GIF up to 5 MB; cropped square, resized to 256 px and re-saved without any metadata (camera or GPS) |
| Contact phone | The person | Optional; US numbers, stored as (508) 531-1000 |
| Password | The person | See below; the administrator can only issue a temporary one |

**Who sees what:** everyone signed in sees the Directory's names, emails, phones, roles and pictures.
Residence halls, inactive accounts and last sign-in times are visible only to the administrator and
the person themselves.

The directory lives on the data share at `security/users.json` (owner-only permissions where the file system supports them). Passwords
are kept only as salted scrypt hashes. Pictures are in `security/avatars/` and served only to
signed-in users.

## Managing accounts (administrator)

From your own computer, against the running app, over HTTPS:

```bash
export WEPA_URL=https://<the app's address>
python -m wepa_monitor users add jsmith --name "Jordan Smith" --email jsmith@bridgew.edu
python -m wepa_monitor users add alee --name "Alex Lee" --email alee@bridgew.edu --resident yes --hall "Scott Hall"
python -m wepa_monitor users update jsmith --role viewer
python -m wepa_monitor users update alee --resident no
python -m wepa_monitor users deactivate jsmith
python -m wepa_monitor users activate jsmith
python -m wepa_monitor users reset jsmith          # temporary password; they choose a new one at sign-in
python -m wepa_monitor users list
python -m wepa_monitor users show jsmith
python -m wepa_monitor users halls                 # residence halls you can assign
```

The tool asks for the administrator (`bsuresnet`) password, or reads `WEPA_ADMIN_PASSWORD`. It refuses
plain `http://` except to `localhost`. New and reset passwords are generated, shown once and
temporary. Every change goes to the System page's audit log. `--local --data-dir …` edits a copy on
your own computer instead.

## Sign-in practices

* **A real sign-in page** with a session, not the browser's pop-up. The session cookie is `HttpOnly`,
  `SameSite=Lax`, and `Secure` over HTTPS. It carries no password, only who you are and a version
  number for revocation.
* **Session limits:** 2 hours idle or 12 hours after signing in, whichever comes first. Sign out from the
  menu under your picture. Changing your password, an administrator reset or deactivation ends every
  other session at once.
* **Passwords (NIST SP 800-63B):** 12 to 128 characters of anything. Paste is allowed, and there are no
  forced symbol rules and no expiry. Rejected: common passwords; ones containing your username, name
  or email, or this service's name; repeated characters or simple sequences; and ones found in known
  data breaches. The breach check uses Have I Been Pwned's k-anonymity range lookup: only the first 5
  characters of the password's SHA-1 hash leave the server. It's skipped if the service can't be
  reached; set `WEPA_BREACH_CHECK=0` to turn it off.
* **Temporary passwords** from the administrator must be changed at the first sign-in.
* **Guessing protection:** 5 wrong passwords for one account in 15 minutes lock that account for 15
  minutes. 8 failures from one network lock the network for 15 minutes. The message never says
  whether a username exists, and a wrong username takes as long to check as a wrong password.
* **Forms:** CSRF tokens on every form, cross-site POSTs refused, and sign-in redirects only to pages
  on this site.
* **Records:** sign-ins, failures (never the password typed), sign-outs, password changes and every
  account change go to the audit log on the System page.
* **Signing key:** `WEPA_SECRET_KEY` if set; otherwise one is generated once and kept, owner-readable
  only, in the security folder, so sessions survive deploys.

## The administrator account

`bsuresnet` (from `WEPA_BASIC_AUTH`) isn't a person in the directory. It signs in on the same page,
can see everything, and is the only account that can manage accounts (through the command-line tool).
It can't change investigations, because every change needs one person's name. Changing its password
in the app's configuration signs out every administrator session.

## Toward university single sign-on

The real "AD" for BSU is Microsoft Entra ID (Azure AD). If BSU IT registers this app, sign-in can
move to Entra ID (OpenID Connect): people would use their BSU credentials and BSU's MFA. Directory
fields like name and email would then come from Entra, and resident status and hall would stay here.
The directory and session design above is built to make that a swap of the sign-in step, not a
rewrite.
