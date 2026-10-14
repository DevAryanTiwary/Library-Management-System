"""SmartLibPro — a local library portal with student and administrator panels."""

from datetime import date, timedelta
from functools import wraps
from io import BytesIO
from pathlib import Path
import hmac
import os
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid

from flask import Flask, g, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from database import (EMAIL_RE, connect_database, get_settings, initialize_database,
                      library_today, money_cents, validate_backup)


LOAN_SELECT = '''SELECT l.*, b.title, b.author, b.isbn, b.category,
    u.name AS student_name, u.email AS student_email, u.student_id
    FROM loans l JOIN books b ON b.id=l.book_id JOIN users u ON u.id=l.user_id'''
BOOK_SELECT = '''SELECT b.*, (SELECT COUNT(*) FROM loans l WHERE l.book_id=b.id
    AND l.returned_at IS NULL) AS issued_copies FROM books b'''


class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def text_field(data, key, minimum=0, maximum=100, default=''):
    value = data.get(key, default)
    if not isinstance(value, str):
        raise APIError(f'{key.replace("_", " ").capitalize()} must be text.')
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        raise APIError(f'{key.replace("_", " ").capitalize()} must contain {minimum}–{maximum} characters.')
    return value


def integer_field(value, label, minimum=1, maximum=10000):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise APIError(f'Enter a whole number for {label}.')
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise APIError(f'Enter a whole number for {label}.') from None
    if not minimum <= number <= maximum:
        raise APIError(f'{label.capitalize()} must be between {minimum} and {maximum}.')
    return number


def email_field(data):
    value = text_field(data, 'email', 3, 254).lower()
    if not EMAIL_RE.fullmatch(value):
        raise APIError('Enter a valid email address.')
    return value


def password_field(data, key='password'):
    value = data.get(key)
    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        raise APIError('Passwords must contain 8–128 characters.')
    return value


def public_user(row):
    result = {key: row[key] for key in ('id', 'name', 'email', 'student_id', 'department', 'role', 'created_at')}
    result['active'] = bool(row['active'])
    return result


def book_data(row):
    result = dict(row)
    result['active'] = bool(result['active'])
    result['available_copies'] = result['total_copies'] - result['issued_copies']
    return result


def loan_data(row):
    result = dict(row)
    today = library_today()
    end_date = date.fromisoformat(row['returned_at']) if row['returned_at'] else today
    days_late = max(0, (end_date - date.fromisoformat(row['due_date'])).days)
    cents = row['fine_cents'] if row['returned_at'] else days_late * row['fine_rate_cents']
    result.update(days_overdue=days_late, fine_per_day=row['fine_rate_cents'] / 100,
                  fine_amount=cents / 100, paid_amount=row['paid_cents'] / 100,
                  balance=(cents - row['paid_cents']) / 100,
                  status='returned' if row['returned_at'] else ('overdue' if days_late else 'active'))
    for field in ('fine_rate_cents', 'fine_cents', 'paid_cents'):
        result.pop(field)
    return result


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=os.environ.get('SMARTLIB_DATABASE', str(Path(app.instance_path) / 'library.db')),
        SECRET_KEY=os.environ.get('SMARTLIB_SECRET_KEY'),
        ADMIN_EMAIL=os.environ.get('SMARTLIB_ADMIN_EMAIL', 'admin@smartlib.local'),
        ADMIN_PASSWORD=os.environ.get('SMARTLIB_ADMIN_PASSWORD', 'Admin@12345'),
        SEED_DEMO=os.environ.get('SMARTLIB_SEED_DEMO', '1') != '0',
        BACKUP_MAX_BYTES=int(os.environ.get('SMARTLIB_MAX_BACKUP_MB', '64')) * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('SMARTLIB_SECURE_COOKIES', '0') == '1',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    if test_config:
        app.config.update(test_config)
    app.config.setdefault('MAX_CONTENT_LENGTH', app.config['BACKUP_MAX_BYTES'] + 1024 * 1024)
    if app.config['MAX_CONTENT_LENGTH'] is None:
        app.config['MAX_CONTENT_LENGTH'] = app.config['BACKUP_MAX_BYTES'] + 1024 * 1024
    if not app.config['SECRET_KEY']:
        # Persist across restarts without committing a cookie signing key to source.
        secret_path = Path(app.config['DATABASE']).parent / '.secret_key'
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with secret_path.open('x', encoding='utf-8') as file:
                file.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        app.config['SECRET_KEY'] = secret_path.read_text(encoding='utf-8').strip()
    initialize_database(app.config['DATABASE'], app.config['ADMIN_EMAIL'],
                        app.config['ADMIN_PASSWORD'], app.config['SEED_DEMO'])
    app.extensions['database_lock'] = threading.RLock()
    app.extensions['login_attempts'] = {}

    def db():
        if 'db' not in g:
            g.db = connect_database(app.config['DATABASE'])
        return g.db

    def csrf_token(rotate=False):
        if rotate or 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        return session['csrf_token']

    def json_body():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise APIError('Send a JSON object with the required fields.')
        return body

    def require_auth(admin=False):
        def decorator(func):
            @wraps(func)
            def wrapped(*args, **kwargs):
                if g.user is None:
                    raise APIError('Please sign in to continue.', 401)
                if admin and g.user['role'] != 'admin':
                    raise APIError('Administrator access is required.', 403)
                return func(*args, **kwargs)
            return wrapped
        return decorator

    def sign_in(row):
        session.clear()
        session.permanent = True
        session['user_id'] = row['id']
        session['auth_version'] = row['auth_version']
        session['epoch'] = db().execute("SELECT value FROM meta WHERE key='session_epoch'").fetchone()[0]
        return {'user': public_user(row), 'csrf_token': csrf_token(True)}

    def find_loan(loan_id):
        row = db().execute(LOAN_SELECT + ' WHERE l.id=?', (loan_id,)).fetchone()
        if row is None:
            raise APIError('Loan not found.', 404)
        if g.user['role'] != 'admin' and row['user_id'] != g.user['id']:
            raise APIError('You can access only your own loans.', 403)
        return row

    def student_identifier(value, conn):
        if value:
            return value
        # IDs stay unique after deletions, restore, and registration races.
        while True:
            candidate = 'STU-' + secrets.token_hex(3).upper()
            if not conn.execute('SELECT 1 FROM users WHERE student_id=?', (candidate,)).fetchone():
                return candidate

    @app.before_request
    def prepare_request():
        if not request.path.startswith('/api/'):
            return
        # Serialize access so restores cannot replace a database during a loan write.
        app.extensions['database_lock'].acquire()
        g.database_locked = True
        g.user = None
        user_id = session.get('user_id')
        if user_id:
            row = db().execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            epoch = db().execute("SELECT value FROM meta WHERE key='session_epoch'").fetchone()[0]
            if row and row['active'] and session.get('epoch') == epoch and session.get('auth_version') == row['auth_version']:
                g.user = row
            else:
                session.clear()
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            expected = session.get('csrf_token', '')
            supplied = request.headers.get('X-CSRF-Token', '')
            if not expected or not supplied.isascii() or not hmac.compare_digest(expected, supplied):
                raise APIError('Your session has expired. Refresh the page and try again.', 403)

    @app.teardown_request
    def close_database(error=None):
        connection = g.pop('db', None)
        if connection:
            connection.close()
        if g.pop('database_locked', False):
            app.extensions['database_lock'].release()

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        return jsonify(error=str(error)), error.status

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict(error):
        return jsonify(error='That email, student ID, ISBN, or active loan already exists. Check your entries.'), 409

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith('/api/'):
            return jsonify(error=error.description), error.code
        return error

    @app.errorhandler(sqlite3.OperationalError)
    def storage_error(error):
        app.logger.exception('Database operation failed')
        return jsonify(error='The library database is busy or unavailable. Please try again.'), 503

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/session')
    def current_session():
        return jsonify(user=public_user(g.user) if g.user else None,
                       csrf_token=csrf_token(), settings=get_settings(db()),
                       today=library_today().isoformat(), demo_available=bool(app.config['SEED_DEMO']),
                       backup_max_mb=app.config['BACKUP_MAX_BYTES'] / (1024 * 1024))

    @app.post('/api/login')
    def login():
        data = json_body()
        email = email_field(data)
        password = data.get('password', '')
        if not isinstance(password, str) or len(password) > 128:
            raise APIError('Invalid email or password.', 401)
        # Local lightweight rate limit; a deployment can add shared gateway limits.
        now = time.monotonic()
        key = request.remote_addr or 'local'
        attempts = app.extensions['login_attempts']
        attempts[key] = [stamp for stamp in attempts.get(key, []) if now - stamp < 300]
        if len(attempts[key]) >= 20:
            raise APIError('Too many sign-in attempts. Please wait five minutes.', 429)
        row = db().execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if row is None or not check_password_hash(row['password_hash'], password) or not row['active']:
            attempts[key].append(now)
            raise APIError('Invalid email or password, or the account is inactive.', 401)
        if data.get('role') and row['role'] != data['role']:
            raise APIError('This account belongs to the other panel. Select the matching role.', 403)
        attempts.pop(key, None)
        return jsonify(sign_in(row))

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(csrf_token=csrf_token(True))

    @app.post('/api/register')
    def register():
        data = json_body()
        name = text_field(data, 'name', 2, 100)
        email = email_field(data)
        password = password_field(data)
        identifier = text_field(data, 'student_id', 0, 40)
        department = text_field(data, 'department', 0, 100)
        connection = db()
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            identifier = student_identifier(identifier, connection)
            cursor = connection.execute("INSERT INTO users(name,email,password_hash,student_id,department,role,created_at) VALUES (?,?,?,?,?,'student',?)",
                                        (name, email, generate_password_hash(password), identifier, department, library_today().isoformat()))
        row = connection.execute('SELECT * FROM users WHERE id=?', (cursor.lastrowid,)).fetchone()
        return jsonify(sign_in(row)), 201

    @app.get('/api/books')
    @require_auth()
    def list_books():
        query = request.args.get('q', '').strip()[:200]
        status = request.args.get('status', 'all')
        rows = db().execute(BOOK_SELECT + ' WHERE b.active=1 AND (b.title LIKE ? OR b.author LIKE ? OR b.isbn LIKE ? OR b.category LIKE ?) ORDER BY b.title COLLATE NOCASE',
                            tuple(['%' + query + '%'] * 4)).fetchall()
        books = [book_data(row) for row in rows]
        if status == 'available':
            books = [book for book in books if book['available_copies'] > 0]
        elif status == 'issued':
            books = [book for book in books if book['issued_copies'] > 0]
        elif status != 'all':
            raise APIError('Choose all, available, or issued books.')
        return jsonify(books=books)

    def book_fields(data, row=None):
        defaults = dict(row) if row is not None else {}
        title = text_field(data, 'title', 1, 200, defaults.get('title', ''))
        author = text_field(data, 'author', 1, 150, defaults.get('author', ''))
        isbn = text_field(data, 'isbn', 0, 32, defaults.get('isbn') or '') or None
        category = text_field(data, 'category', 0, 60, defaults.get('category', 'General')) or 'General'
        description = text_field(data, 'description', 0, 2000, defaults.get('description', ''))
        copies = integer_field(data.get('total_copies', defaults.get('total_copies', 1)), 'total copies')
        return title, author, isbn, category, description, copies

    @app.post('/api/books')
    @require_auth(admin=True)
    def create_book():
        values = book_fields(json_body())
        with db():
            cursor = db().execute('INSERT INTO books(title,author,isbn,category,description,total_copies,created_at) VALUES (?,?,?,?,?,?,?)',
                                  values + (library_today().isoformat(),))
        return jsonify(book=book_data(db().execute(BOOK_SELECT + ' WHERE b.id=?', (cursor.lastrowid,)).fetchone())), 201

    @app.put('/api/books/<int:book_id>')
    @require_auth(admin=True)
    def update_book(book_id):
        data = json_body()
        connection = db()
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(BOOK_SELECT + ' WHERE b.id=? AND b.active=1', (book_id,)).fetchone()
            if not row:
                raise APIError('Book not found.', 404)
            values = book_fields(data, row)
            if values[-1] < row['issued_copies']:
                raise APIError('Total copies cannot be lower than the number currently issued.', 409)
            connection.execute('UPDATE books SET title=?,author=?,isbn=?,category=?,description=?,total_copies=? WHERE id=?', values + (book_id,))
        return jsonify(book=book_data(connection.execute(BOOK_SELECT + ' WHERE b.id=?', (book_id,)).fetchone()))

    @app.delete('/api/books/<int:book_id>')
    @require_auth(admin=True)
    def archive_book(book_id):
        connection = db()
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(BOOK_SELECT + ' WHERE b.id=? AND b.active=1', (book_id,)).fetchone()
            if not row:
                raise APIError('Book not found.', 404)
            if row['issued_copies']:
                raise APIError('Return all issued copies before archiving this book.', 409)
            connection.execute('UPDATE books SET active=0 WHERE id=?', (book_id,))
        return jsonify(message='Book archived. Loan history has been preserved.')

    @app.get('/api/users')
    @require_auth(admin=True)
    def list_users():
        query = '%' + request.args.get('q', '').strip()[:200] + '%'
        rows = db().execute('SELECT * FROM users WHERE name LIKE ? OR email LIKE ? OR student_id LIKE ? ORDER BY active DESC, name COLLATE NOCASE',
                            (query, query, query)).fetchall()
        balances = {}
        active_counts = {}
        for row in db().execute(LOAN_SELECT):
            loan = loan_data(row)
            balances[row['user_id']] = balances.get(row['user_id'], 0) + round(loan['balance'] * 100)
            if row['returned_at'] is None:
                active_counts[row['user_id']] = active_counts.get(row['user_id'], 0) + 1
        users = []
        for row in rows:
            user = public_user(row)
            user.update(active_loans=active_counts.get(row['id'], 0), unpaid_fines=balances.get(row['id'], 0) / 100)
            users.append(user)
        return jsonify(users=users)

    @app.post('/api/users')
    @require_auth(admin=True)
    def create_user():
        data = json_body()
        role = data.get('role', 'student')
        if role not in ('student', 'admin'):
            raise APIError('Choose student or admin for the account role.')
        name, email, password = text_field(data, 'name', 2, 100), email_field(data), password_field(data)
        department = text_field(data, 'department', 0, 100)
        identifier = text_field(data, 'student_id', 0, 40)
        connection = db()
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            identifier = student_identifier(identifier, connection) if role == 'student' else None
            cursor = connection.execute('INSERT INTO users(name,email,password_hash,student_id,department,role,created_at) VALUES (?,?,?,?,?,?,?)',
                                        (name, email, generate_password_hash(password), identifier, department, role, library_today().isoformat()))
        return jsonify(user=public_user(connection.execute('SELECT * FROM users WHERE id=?', (cursor.lastrowid,)).fetchone())), 201

    def guard_deactivation(row, active):
        if active:
            return
        if row['id'] == g.user['id']:
            raise APIError('You cannot deactivate your own administrator account.', 409)
        if row['role'] == 'admin' and row['active'] and db().execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
            raise APIError('The library must retain an active administrator.', 409)

    @app.put('/api/users/<int:user_id>')
    @require_auth(admin=True)
    def update_user(user_id):
        data = json_body()
        connection = db()
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if not row:
                raise APIError('User not found.', 404)
            name = text_field(data, 'name', 2, 100, row['name'])
            email = email_field({'email': data.get('email', row['email'])})
            identifier = text_field(data, 'student_id', 0, 40, row['student_id'] or '')
            department = text_field(data, 'department', 0, 100, row['department'])
            active = data.get('active', bool(row['active']))
            if active not in (True, False, 0, 1):
                raise APIError('Account status must be active or inactive.')
            guard_deactivation(row, active)
            new_hash = row['password_hash']
            version = row['auth_version']
            if data.get('password'):
                new_hash = generate_password_hash(password_field(data))
                version += 1
            if email != row['email'] or active != bool(row['active']):
                version += 1
            identifier = student_identifier(identifier, connection) if row['role'] == 'student' else None
            connection.execute('UPDATE users SET name=?,email=?,student_id=?,department=?,active=?,password_hash=?,auth_version=? WHERE id=?',
                               (name, email, identifier, department, int(active), new_hash, version, user_id))
        if user_id == g.user['id']:
            session['auth_version'] = version
        return jsonify(user=public_user(connection.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()))

    @app.delete('/api/users/<int:user_id>')
    @require_auth(admin=True)
    def deactivate_user(user_id):
        with db():
            db().execute('BEGIN IMMEDIATE')
            row = db().execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if not row:
                raise APIError('User not found.', 404)
            guard_deactivation(row, False)
            db().execute('UPDATE users SET active=0,auth_version=auth_version+1 WHERE id=?', (user_id,))
        return jsonify(message='Account deactivated. Records and loan history have been preserved.')

    @app.put('/api/profile')
    @require_auth()
    def update_profile():
        data = json_body()
        name = text_field(data, 'name', 2, 100, g.user['name'])
        department = text_field(data, 'department', 0, 100, g.user['department'])
        new_hash = g.user['password_hash']
        version = g.user['auth_version']
        if data.get('new_password'):
            current = data.get('current_password', '')
            if not isinstance(current, str) or not check_password_hash(new_hash, current):
                raise APIError('Your current password is incorrect.', 403)
            new_hash = generate_password_hash(password_field(data, 'new_password'))
            version += 1
        with db():
            db().execute('UPDATE users SET name=?,department=?,password_hash=?,auth_version=? WHERE id=?',
                         (name, department, new_hash, version, g.user['id']))
        session['auth_version'] = version
        return jsonify(user=public_user(db().execute('SELECT * FROM users WHERE id=?', (g.user['id'],)).fetchone()))

    @app.get('/api/loans')
    @require_auth()
    def list_loans():
        query = '%' + request.args.get('q', '').strip()[:200] + '%'
        sql = LOAN_SELECT + ' WHERE (b.title LIKE ? OR b.author LIKE ? OR u.name LIKE ? OR u.email LIKE ? OR u.student_id LIKE ?)'
        args = [query] * 5
        if g.user['role'] != 'admin':
            sql += ' AND l.user_id=?'
            args.append(g.user['id'])
        rows = db().execute(sql + ' ORDER BY l.id DESC', args).fetchall()
        loans = [loan_data(row) for row in rows]
        status = request.args.get('status', 'all')
        if status == 'active':
            loans = [loan for loan in loans if loan['returned_at'] is None]
        elif status in ('overdue', 'returned'):
            loans = [loan for loan in loans if loan['status'] == status]
        elif status != 'all':
            raise APIError('Choose all, active, overdue, or returned loans.')
        return jsonify(loans=loans)

    @app.post('/api/loans')
    @require_auth()
    def issue_book():
        data = json_body()
        book_id = integer_field(data.get('book_id'), 'book ID', maximum=2147483647)
        admin = g.user['role'] == 'admin'
        user_id = integer_field(data.get('user_id'), 'student ID', maximum=2147483647) if admin else g.user['id']
        if not admin and 'user_id' in data and data['user_id'] != g.user['id']:
            raise APIError('You can borrow only for your own account.', 403)
        today = library_today()
        connection = db()
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            settings = get_settings(connection)
            due = today + timedelta(days=settings['loan_days'])
            if admin and data.get('due_date'):
                try:
                    due = date.fromisoformat(data['due_date'])
                except (ValueError, TypeError):
                    raise APIError('Enter a valid due date in YYYY-MM-DD format.') from None
                if not today <= due <= today + timedelta(days=365):
                    raise APIError('The due date must be today or within the next year.')
            elif not admin and data.get('due_date') and data['due_date'] != due.isoformat():
                raise APIError('Student loans use the library lending period.', 403)
            user = connection.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if not user or user['role'] != 'student' or not user['active']:
                raise APIError('Choose an active student account.')
            book = connection.execute(BOOK_SELECT + ' WHERE b.id=? AND b.active=1', (book_id,)).fetchone()
            if not book:
                raise APIError('Book not found.', 404)
            if book['issued_copies'] >= book['total_copies']:
                raise APIError('All copies of this book are currently issued.', 409)
            if connection.execute('SELECT 1 FROM loans WHERE user_id=? AND book_id=? AND returned_at IS NULL', (user_id, book_id)).fetchone():
                raise APIError('This student already has an active loan for this book.', 409)
            count = connection.execute('SELECT COUNT(*) FROM loans WHERE user_id=? AND returned_at IS NULL', (user_id,)).fetchone()[0]
            if count >= settings['max_loans']:
                raise APIError(f'This student has reached the limit of {settings["max_loans"]} active loans.', 409)
            cursor = connection.execute('INSERT INTO loans(book_id,user_id,issued_at,due_date,fine_rate_cents) VALUES (?,?,?,?,?)',
                                        (book_id, user_id, today.isoformat(), due.isoformat(), money_cents(settings['fine_per_day'])))
        return jsonify(loan=loan_data(connection.execute(LOAN_SELECT + ' WHERE l.id=?', (cursor.lastrowid,)).fetchone())), 201

    @app.post('/api/loans/<int:loan_id>/return')
    @require_auth()
    def return_book(loan_id):
        connection = db()
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            row = find_loan(loan_id)
            if row['returned_at']:
                raise APIError('This book has already been returned.', 409)
            today = library_today()
            fine = max(0, (today - date.fromisoformat(row['due_date'])).days) * row['fine_rate_cents']
            connection.execute('UPDATE loans SET returned_at=?,fine_cents=? WHERE id=?', (today.isoformat(), fine, loan_id))
        return jsonify(loan=loan_data(connection.execute(LOAN_SELECT + ' WHERE l.id=?', (loan_id,)).fetchone()))

    @app.post('/api/loans/<int:loan_id>/pay')
    @require_auth(admin=True)
    def settle_fine(loan_id):
        with db():
            db().execute('BEGIN IMMEDIATE')
            row = find_loan(loan_id)
            if row['returned_at'] is None:
                raise APIError('Return the book to finalize its fine before recording payment.', 409)
            if row['fine_cents'] <= row['paid_cents']:
                raise APIError('This loan has no outstanding fine.', 409)
            db().execute('UPDATE loans SET paid_cents=fine_cents WHERE id=?', (loan_id,))
        return jsonify(loan=loan_data(db().execute(LOAN_SELECT + ' WHERE l.id=?', (loan_id,)).fetchone()))

    @app.get('/api/dashboard')
    @require_auth()
    def dashboard():
        connection = db()
        sql, args = LOAN_SELECT, []
        if g.user['role'] != 'admin':
            sql += ' WHERE l.user_id=?'
            args.append(g.user['id'])
        loans = [loan_data(row) for row in connection.execute(sql + ' ORDER BY l.id DESC', args)]
        available = connection.execute('SELECT SUM(total_copies) FROM books WHERE active=1').fetchone()[0] or 0
        global_active = connection.execute('SELECT COUNT(*) FROM loans WHERE returned_at IS NULL').fetchone()[0]
        active = [loan for loan in loans if loan['returned_at'] is None]
        stats = dict(total_books=connection.execute('SELECT COUNT(*) FROM books WHERE active=1').fetchone()[0],
                     total_copies=available, available_copies=available - global_active,
                     active_loans=len(active), overdue_loans=sum(loan['status'] == 'overdue' for loan in active),
                     total_students=connection.execute("SELECT COUNT(*) FROM users WHERE role='student' AND active=1").fetchone()[0],
                     unpaid_fines=sum(round(loan['balance'] * 100) for loan in loans) / 100,
                     returned_loans=len(loans) - len(active))
        popular = connection.execute(BOOK_SELECT + ' WHERE b.active=1 ORDER BY (SELECT COUNT(*) FROM loans WHERE book_id=b.id) DESC,b.title LIMIT 4').fetchall()
        horizon = (library_today() + timedelta(days=3)).isoformat()
        upcoming = [loan for loan in active if loan['due_date'] <= horizon]
        return jsonify(stats=stats, recent_loans=loans[:6], due_soon=sorted(upcoming, key=lambda loan: loan['due_date'])[:5],
                       popular_books=[book_data(row) for row in popular], settings=get_settings(connection))

    @app.get('/api/settings')
    @require_auth()
    def read_settings():
        return jsonify(settings=get_settings(db()))

    @app.put('/api/settings')
    @require_auth(admin=True)
    def update_settings():
        data = json_body()
        current = get_settings(db())
        loan_days = integer_field(data.get('loan_days', current['loan_days']), 'loan days', maximum=365)
        max_loans = integer_field(data.get('max_loans', current['max_loans']), 'maximum loans', maximum=50)
        try:
            rate = money_cents(data.get('fine_per_day', current['fine_per_day']))
        except ValueError as exc:
            raise APIError(str(exc)) from None
        with db():
            for key, value in [('loan_days', str(loan_days)), ('max_loans', str(max_loans)), ('fine_per_day', f'{rate / 100:.2f}')]:
                db().execute('UPDATE settings SET value=? WHERE key=?', (value, key))
        return jsonify(settings=get_settings(db()), message='Lending policy saved. Existing loans retain their original due dates and fine rates.')

    def snapshot(destination):
        target = sqlite3.connect(str(destination))
        try:
            db().backup(target)
        finally:
            target.close()

    @app.get('/api/backup')
    @require_auth(admin=True)
    def download_backup():
        with tempfile.TemporaryDirectory(prefix='smartlib-backup-') as directory:
            path = Path(directory) / 'backup.sqlite3'
            snapshot(path)
            if path.stat().st_size > app.config['BACKUP_MAX_BYTES']:
                raise APIError('This database exceeds the configured backup limit. Increase SMARTLIB_MAX_BACKUP_MB and restart the app before backing up.')
            contents = BytesIO(path.read_bytes())
        return send_file(contents, mimetype='application/vnd.sqlite3', as_attachment=True,
                         download_name=f'smartlib-backup-{library_today().isoformat()}.sqlite3')

    @app.post('/api/restore')
    @require_auth(admin=True)
    def restore_backup():
        if request.form.get('confirmation') != 'RESTORE':
            raise APIError('Type RESTORE to confirm replacing the current library data.')
        password = request.form.get('password', '')
        if not check_password_hash(g.user['password_hash'], password):
            raise APIError('Your administrator password is incorrect.', 403)
        upload = request.files.get('file')
        if upload is None or not upload.filename:
            raise APIError('Choose a SmartLibPro backup file.')
        with tempfile.TemporaryDirectory(prefix='smartlib-restore-') as directory:
            source = Path(directory) / 'uploaded.sqlite3'
            upload.save(source)
            if source.stat().st_size > app.config['BACKUP_MAX_BYTES']:
                limit_mb = app.config['BACKUP_MAX_BYTES'] / (1024 * 1024)
                raise APIError(f'Backups must be smaller than {limit_mb:g} MB. Increase SMARTLIB_MAX_BACKUP_MB to support a larger library.')
            try:
                validate_backup(source)
            except (ValueError, sqlite3.DatabaseError) as exc:
                raise APIError(str(exc) if isinstance(exc, ValueError) else 'The backup is damaged or incompatible.') from None
            # Persist a recovery copy before the atomic replacement.
            target_path = Path(app.config['DATABASE']).resolve()
            safety_dir = target_path.parent / 'backups'
            safety_dir.mkdir(parents=True, exist_ok=True)
            safety = safety_dir / f'pre-restore-{library_today().isoformat()}-{uuid.uuid4().hex[:8]}.sqlite3'
            snapshot(safety)
            staging = target_path.with_name(f'.restore-{uuid.uuid4().hex}.sqlite3')
            try:
                imported = connect_database(source)
                try:
                    with imported:
                        imported.execute("UPDATE meta SET value=? WHERE key='session_epoch'", (uuid.uuid4().hex,))
                    destination = sqlite3.connect(str(staging))
                    try:
                        imported.backup(destination)
                    finally:
                        destination.close()
                finally:
                    imported.close()
                g.pop('db').close()
                os.replace(staging, target_path)
            finally:
                staging.unlink(missing_ok=True)
        session.clear()
        return jsonify(message='Backup restored. Sign in with an account from the restored backup. A safety backup of the previous data was saved.',
                       csrf_token=csrf_token(True))

    return app


if __name__ == '__main__':
    application = create_app()
    application.run(host=os.environ.get('SMARTLIB_HOST', '127.0.0.1'),
                    port=int(os.environ.get('SMARTLIB_PORT', '5000')), debug=False)
