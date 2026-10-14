# SmartLibPro

A Flask and SQLite library management app with separate student and administrator panels.

## Run locally

Python 3.10 or newer is recommended. From this folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

To start the server and open the app in your browser together after installing dependencies, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

If PowerShell prevents activation, use `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` and `.\.venv\Scripts\python.exe app.py` instead.

On macOS or Linux, activate with `source .venv/bin/activate`; the install and run commands are the same. Open http://127.0.0.1:5000 in your browser. The app creates its database automatically at `instance/library.db`.

## Demo accounts

| Panel | Email | Password |
| --- | --- | --- |
| Administrator | `admin@smartlib.local` | `Admin@12345` |
| Student | `student@smartlib.local` | `Student@12345` |

Change these passwords from the profile page before using real records. Demo data is intended for local evaluation.

## Features

Students can search books by title or author, see available and issued copies, issue available books, return their loans, and view due dates, overdue fines, and loan history. The default policy allows five active loans per student for 14 days, with a late fine of ₹5 per day. A loan keeps the fine rate set when it was issued; subsequent policy changes apply to new loans.

Administrators can maintain book and student records, issue or return books on a student's behalf, review current loans and overdue books, settle fines on returned loans, and change lending policy. Archiving a book preserves its history and is blocked while it has active loans. Deactivating a student prevents access while preserving their records.

The administrator backup page downloads a SQLite snapshot as a `.sqlite3` file. Download and restore support backups up to 64 MB by default; set `SMARTLIB_MAX_BACKUP_MB` to a larger limit before starting the app if needed. Restore checks database compatibility and integrity, requires the administrator's current password and the exact confirmation `RESTORE`, creates a safety backup in `instance/backups` (or `backups` beside a custom database), and logs out all sessions. Store downloaded backups somewhere separate from this computer; a restore replaces the current data.

## Configuration

Set environment variables before starting the app. In PowerShell, for example:

```powershell
$env:SMARTLIB_SECRET_KEY = 'replace-with-a-long-random-secret'
$env:SMARTLIB_ADMIN_EMAIL = 'librarian@example.com'
$env:SMARTLIB_ADMIN_PASSWORD = 'replace-with-a-strong-password'
$env:SMARTLIB_SEED_DEMO = '0'
python app.py
```

| Variable | Purpose |
| --- | --- |
| `SMARTLIB_DATABASE` | Optional absolute path to the SQLite database; defaults to `instance/library.db`. |
| `SMARTLIB_SECRET_KEY` | Stable, strong session secret for deployment. |
| `SMARTLIB_ADMIN_EMAIL` / `SMARTLIB_ADMIN_PASSWORD` | Credentials for the first administrator when initializing a new database. |
| `SMARTLIB_SEED_DEMO` | Set to `0` to skip sample books and the demo student; an administrator is still created. |
| `SMARTLIB_HOST` | Listening address; defaults to `127.0.0.1`. |
| `SMARTLIB_PORT` | Listening port; defaults to `5000`. |
| `SMARTLIB_SECURE_COOKIES` | Set to `1` when serving over HTTPS to require HTTPS for session cookies. Leave unset for local HTTP. |
| `SMARTLIB_MAX_BACKUP_MB` | Maximum supported backup size in MB; defaults to `64` and applies to both download and restore. |

Changing the bootstrap credentials does not change an existing account. Keep the database, backups, and secret out of source control. The built-in server runs without debug mode and is for local use; deploy with a production WSGI server, HTTPS, restricted file access, a strong secret, and unique administrator credentials when serving other users. The application factory is `app:create_app()`.

Run one app process; threads are supported. Restore serializes requests within that process, so multiple WSGI workers or separate processes sharing the same database are not supported.

## Checks

Run the automated tests from this folder:

```powershell
python -m unittest discover -s tests -v
```

An optional browser check uses Node.js 22 or newer and an installed Chrome browser. It checks both panels and mobile layout using a temporary database and saves screenshots to `artifacts`:

```powershell
node tests/browser_smoke.mjs
```

Set `CHROME_PATH` to the Chrome executable if it is installed somewhere other than `C:\Program Files\Google\Chrome\Application\chrome.exe`.

SQLite is suitable for a small library on a single app instance. Keep regular backups and test a restore before relying on them.
