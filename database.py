"""SQLite storage, seed data, and portable library backup validation."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sqlite3
import uuid

from werkzeug.security import generate_password_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 2 AND 100),
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    student_id TEXT UNIQUE COLLATE NOCASE,
    department TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK(role IN ('admin', 'student')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    auth_version INTEGER NOT NULL DEFAULT 1 CHECK(auth_version > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
    author TEXT NOT NULL CHECK(length(author) BETWEEN 1 AND 150),
    isbn TEXT UNIQUE,
    category TEXT NOT NULL DEFAULT 'General',
    description TEXT NOT NULL DEFAULT '',
    total_copies INTEGER NOT NULL CHECK(total_copies BETWEEN 1 AND 10000),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    issued_at TEXT NOT NULL,
    due_date TEXT NOT NULL,
    returned_at TEXT,
    fine_rate_cents INTEGER NOT NULL CHECK(fine_rate_cents BETWEEN 0 AND 100000),
    fine_cents INTEGER NOT NULL DEFAULT 0 CHECK(fine_cents >= 0),
    paid_cents INTEGER NOT NULL DEFAULT 0 CHECK(paid_cents >= 0 AND paid_cents <= fine_cents),
    CHECK(due_date >= issued_at),
    CHECK(returned_at IS NULL OR returned_at >= issued_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_loan_per_book
    ON loans(user_id, book_id) WHERE returned_at IS NULL;
CREATE INDEX IF NOT EXISTS loans_by_book ON loans(book_id, returned_at);
CREATE INDEX IF NOT EXISTS loans_by_user ON loans(user_id, returned_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

DEFAULT_SETTINGS = {'loan_days': '14', 'max_loans': '5', 'fine_per_day': '5.00'}
EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def library_today():
    """Calendar dates follow India time even on hosts without zoneinfo data."""
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).date()


def connect_database(path):
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 15000')
    return conn


def money_cents(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 1000:
            raise ValueError('The daily fine must be between 0 and 1000.')
        cents = number * 100
        if cents != cents.to_integral_value():
            raise ValueError('Use at most two decimal places for the daily fine.')
        return int(cents)
    except (InvalidOperation, TypeError):
        raise ValueError('Enter a valid daily fine.') from None


def valid_password_hash(value):
    """Bound imported hashing parameters and reject hashes that cannot be used."""
    if not isinstance(value, str):
        return False
    parts = value.split('$')
    if len(parts) != 3 or not re.fullmatch(r'[A-Za-z0-9]{1,64}', parts[1]):
        return False
    method, salt, digest = parts
    if not re.fullmatch(r'[0-9a-f]+', digest):
        return False
    scrypt = re.fullmatch(r'scrypt:(\d+):(\d+):(\d+)', method)
    if scrypt:
        n, r, p = map(int, scrypt.groups())
        return (1024 <= n <= 65536 and n & (n - 1) == 0 and 1 <= r <= 16
                and 1 <= p <= 2 and n * r <= 524288 and len(digest) == 128)
    pbkdf2 = re.fullmatch(r'pbkdf2:(sha256|sha512):(\d+)', method)
    if pbkdf2:
        algorithm, iterations = pbkdf2.groups()
        return 1 <= int(iterations) <= 2000000 and len(digest) == (64 if algorithm == 'sha256' else 128)
    return False


def get_settings(conn):
    values = dict(conn.execute('SELECT key, value FROM settings').fetchall())
    return {'loan_days': int(values['loan_days']), 'max_loans': int(values['max_loans']),
            'fine_per_day': float(Decimal(values['fine_per_day']))}


def initialize_database(path, admin_email, admin_password, seed_demo=True):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect_database(path)
    try:
        conn.executescript(SCHEMA)
        with conn:
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute('INSERT OR IGNORE INTO settings VALUES (?, ?)', (key, value))
            conn.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('schema_version', '1'))
            conn.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('session_epoch', uuid.uuid4().hex))
            if not conn.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                if (not isinstance(admin_email, str) or not 3 <= len(admin_email) <= 254
                        or not EMAIL_RE.fullmatch(admin_email) or not isinstance(admin_password, str)
                        or not 8 <= len(admin_password) <= 128):
                    raise ValueError('Bootstrap admin requires a valid email and a password of 8–128 characters.')
                today = library_today()
                conn.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES (?,?,?,'admin',?)",
                             ('Alex Morgan', admin_email.lower(), generate_password_hash(admin_password), today.isoformat()))
                if seed_demo:
                    seed_library(conn, today)
    finally:
        conn.close()


def seed_library(conn, today):
    """A browsable sample library and clearly documented demo student account."""
    students = [
        ('Jamie Parker', 'student@smartlib.local', 'STU-1001', 'Computer Science'),
        ('Priya Sharma', 'priya@smartlib.local', 'STU-1002', 'Literature'),
        ('Arjun Mehta', 'arjun@smartlib.local', 'STU-1003', 'Engineering'),
        ('Sofia Chen', 'sofia@smartlib.local', 'STU-1004', 'Design'),
    ]
    password_hash = generate_password_hash('Student@12345')
    for name, email, student_id, department in students:
        conn.execute("INSERT INTO users(name,email,password_hash,student_id,department,role,created_at) VALUES (?,?,?,?,?,'student',?)",
                     (name, email, password_hash, student_id, department, today.isoformat()))
    books = [
        ('Atomic Habits', 'James Clear', '9780735211292', 'Personal Growth', 5, 'A practical guide to building better habits, one small step at a time.'),
        ('The Psychology of Money', 'Morgan Housel', '9780857197689', 'Finance', 4, 'Timeless lessons on wealth, greed, and happiness.'),
        ('Clean Code', 'Robert C. Martin', '9780132350884', 'Technology', 3, 'A handbook of principles and practices for writing readable software.'),
        ('The Midnight Library', 'Matt Haig', '9780525559474', 'Fiction', 4, 'An imaginative novel about possibility, choices, and the lives we might lead.'),
        ('Sapiens', 'Yuval Noah Harari', '9780062316097', 'History', 3, 'A brief history of humankind, from early humans to the modern world.'),
        ('Design of Everyday Things', 'Don Norman', '9780465050659', 'Design', 2, 'Discover how thoughtful design makes everyday objects easier to use.'),
        ('Deep Work', 'Cal Newport', '9781455586691', 'Personal Growth', 4, 'Rules for focused success in a distracted world.'),
        ('The Alchemist', 'Paulo Coelho', '9780061122415', 'Fiction', 5, 'A young shepherd follows a journey of discovery and his personal legend.'),
        ('A Brief History of Time', 'Stephen Hawking', '9780553380163', 'Science', 2, 'Explore the universe, black holes, and the nature of time.'),
        ('Introduction to Algorithms', 'Thomas H. Cormen', '9780262046305', 'Technology', 2, 'A comprehensive reference on algorithms and computational problem solving.'),
        ('Educated', 'Tara Westover', '9780399590504', 'Biography', 3, 'A memoir of education, family, and finding an independent voice.'),
        ('The Great Gatsby', 'F. Scott Fitzgerald', '9780743273565', 'Fiction', 4, 'A classic novel of ambition and longing in the Jazz Age.'),
    ]
    for title, author, isbn, category, copies, description in books:
        conn.execute('INSERT INTO books(title,author,isbn,category,total_copies,description,created_at) VALUES (?,?,?,?,?,?,?)',
                     (title, author, isbn, category, copies, description, today.isoformat()))
    demo_loans = [(1, 2, -10, 4, None), (3, 2, -20, -6, None),
                  (4, 3, -4, 10, None), (5, 4, -16, -2, None),
                  (6, 5, -7, 7, None), (7, 2, -30, -16, -13)]
    for book_id, user_id, issued, due, returned in demo_loans:
        returned_date = (today + timedelta(days=returned)).isoformat() if returned is not None else None
        fine = max(0, returned - due) * 500 if returned is not None else 0
        conn.execute('INSERT INTO loans(book_id,user_id,issued_at,due_date,returned_at,fine_rate_cents,fine_cents) VALUES (?,?,?,?,?,?,?)',
                     (book_id, user_id, (today + timedelta(days=issued)).isoformat(),
                      (today + timedelta(days=due)).isoformat(), returned_date, 500, fine))


def schema_signature(conn):
    return {row[0]: re.sub(r'\s+', ' ', row[1]).strip().replace('IF NOT EXISTS ', '')
            for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def validate_backup(path):
    """Accept only our schema and consistent records, never arbitrary SQLite SQL."""
    with open(path, 'rb') as file:
        if file.read(16) != b'SQLite format 3\x00':
            raise ValueError('Choose a SQLite backup downloaded from SmartLibPro.')
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only = ON')
        conn.execute('PRAGMA trusted_schema = OFF')
        reference = sqlite3.connect(':memory:')
        try:
            reference.executescript(SCHEMA)
            if schema_signature(conn) != schema_signature(reference):
                raise ValueError('The backup has an incompatible database schema.')
        finally:
            reference.close()
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('The backup contains damaged or inconsistent records.')
        meta = dict(conn.execute('SELECT key,value FROM meta').fetchall())
        if meta.get('schema_version') != '1' or set(meta) != {'schema_version', 'session_epoch'}:
            raise ValueError('The backup format is not supported.')
        settings = dict(conn.execute('SELECT key,value FROM settings').fetchall())
        if set(settings) != set(DEFAULT_SETTINGS):
            raise ValueError('The backup has missing settings.')
        if not 1 <= int(settings['loan_days']) <= 365 or not 1 <= int(settings['max_loans']) <= 50:
            raise ValueError('The backup has invalid lending settings.')
        money_cents(settings['fine_per_day'])
        if not conn.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
            raise ValueError('The backup must contain an active administrator.')
        for row in conn.execute('SELECT * FROM users'):
            if not 3 <= len(row['email']) <= 254 or not EMAIL_RE.fullmatch(row['email']) or not valid_password_hash(row['password_hash']):
                raise ValueError('The backup contains invalid user records.')
            date.fromisoformat(row['created_at'])
        for row in conn.execute('SELECT created_at FROM books'):
            date.fromisoformat(row['created_at'])
        for row in conn.execute('SELECT * FROM loans'):
            issued = date.fromisoformat(row['issued_at'])
            due = date.fromisoformat(row['due_date'])
            returned = date.fromisoformat(row['returned_at']) if row['returned_at'] else None
            if issued > library_today() or due < issued or (returned and (returned < issued or returned > library_today())):
                raise ValueError('The backup contains invalid loan dates.')
            fine = max(0, (returned - due).days) * row['fine_rate_cents'] if returned else 0
            if row['fine_cents'] != fine or (returned is None and row['paid_cents']):
                raise ValueError('The backup contains inconsistent fines.')
        over_capacity = conn.execute('SELECT b.id FROM books b JOIN loans l ON b.id=l.book_id AND l.returned_at IS NULL GROUP BY b.id HAVING COUNT(*) > b.total_copies OR b.active=0').fetchone()
        wrong_role = conn.execute("SELECT l.id FROM loans l JOIN users u ON l.user_id=u.id WHERE u.role!='student' LIMIT 1").fetchone()
        if over_capacity or wrong_role:
            raise ValueError('The backup contains inconsistent lending records.')
    except (sqlite3.DatabaseError, OverflowError, TypeError) as exc:
        raise ValueError('The backup could not be read as a valid SmartLibPro database.') from exc
    finally:
        conn.close()
