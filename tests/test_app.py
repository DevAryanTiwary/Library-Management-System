"""Integration tests for SmartLibPro's authentication and lending workflows.

Run from the project directory with ``python -m unittest discover -s tests -v``.
Each test uses an isolated SQLite database and Flask's real request handlers.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app


class LibraryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = str(Path(self.temp.name) / "library.db")
        self.app = create_app({
            "TESTING": True,
            "DATABASE": self.database,
            "SECRET_KEY": "integration-tests-only",
            "SEED_DEMO": False,
        })
        self.admin = self.app.test_client()
        self.login(self.admin, "admin@smartlib.local", "Admin@12345")

    def token(self, client):
        response = client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        token = response.get_json().get("csrf_token")
        self.assertTrue(token)
        return token

    def mutate(self, client, method, path, payload=None, **kwargs):
        headers = {"X-CSRF-Token": self.token(client)}
        return client.open(path, method=method, json=payload, headers=headers, **kwargs)

    def login(self, client, email, password):
        response = self.mutate(client, "POST", "/api/login", {
            "email": email, "password": password,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json().get("csrf_token"))
        return response.get_json()

    def create_book(self, title="The Test Book", author="Test Author", copies=2, **overrides):
        payload = {
            "title": title,
            "author": author,
            "isbn": "",
            "category": "Computing",
            "description": "A book used by integration tests.",
            "total_copies": copies,
        }
        payload.update(overrides)
        response = self.mutate(self.admin, "POST", "/api/books", payload)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["book"]

    def create_student(self, suffix="one", **overrides):
        payload = {
            "name": f"Student {suffix}",
            "email": f"{suffix}@example.test",
            "password": "Student@123",
            "student_id": f"SID-{suffix}",
            "department": "Computer Science",
            "role": "student",
        }
        payload.update(overrides)
        response = self.mutate(self.admin, "POST", "/api/users", payload)
        self.assertEqual(response.status_code, 201, response.get_json())
        student = response.get_json()["user"]
        client = self.app.test_client()
        self.login(client, payload["email"], payload["password"])
        return student, client

    def issue(self, client, book_id, **extra):
        response = self.mutate(client, "POST", "/api/loans", {"book_id": book_id, **extra})
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["loan"]

    def return_loan(self, client, loan_id):
        response = self.mutate(client, "POST", f"/api/loans/{loan_id}/return", {})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["loan"]

    def books(self, client=None, query=""):
        response = (client or self.admin).get("/api/books" + query)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["books"]

    def loans(self, client=None, query=""):
        response = (client or self.admin).get("/api/loans" + query)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["loans"]

    def settings(self, **changes):
        response = self.admin.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        values = response.get_json()["settings"]
        values.update(changes)
        response = self.mutate(self.admin, "PUT", "/api/settings", values)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["settings"]

    def assert_rejected(self, response, codes=(400, 403, 409, 422)):
        self.assertIn(response.status_code, codes, response.get_json())
        self.assertTrue(response.get_json().get("error"))

    def make_overdue(self, loan_id, days):
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE loans SET issued_at = ?, due_date = ? WHERE id = ?", (
                (today - timedelta(days=days + 14)).isoformat(),
                (today - timedelta(days=days)).isoformat(), loan_id,
            ))
        return today

    def test_registration_login_and_password_not_exposed(self):
        client = self.app.test_client()
        response = self.mutate(client, "POST", "/api/register", {
            "name": "New Student",
            "email": "new@example.test",
            "password": "Student@123",
            "student_id": "NEW-001",
            "department": "Science",
            "role": "admin",
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        self.login(client, "new@example.test", "Student@123")
        user = client.get("/api/session").get_json()["user"]
        self.assertEqual(user["role"], "student")
        self.assertEqual(user["email"], "new@example.test")
        self.assertNotIn("password", user)
        self.assertNotIn("password_hash", user)
        duplicate = self.mutate(self.app.test_client(), "POST", "/api/register", {
            "name": "Duplicate", "email": "NEW@example.test", "password": "Student@123",
        })
        self.assert_rejected(duplicate)
        bad_login = self.mutate(self.app.test_client(), "POST", "/api/login", {
            "email": "new@example.test", "password": "WrongPassword",
        })
        self.assertEqual(bad_login.status_code, 401)

    def test_csrf_is_required_and_rotates_after_login(self):
        client = self.app.test_client()
        old_token = self.token(client)
        response = client.post("/api/login", json={
            "email": "admin@smartlib.local", "password": "Admin@12345",
        })
        self.assert_rejected(response)
        login = self.login(client, "admin@smartlib.local", "Admin@12345")
        self.assertNotEqual(login["csrf_token"], old_token)
        response = client.post("/api/books", json={
            "title": "Untrusted", "author": "Author", "total_copies": 1,
        }, headers={"X-CSRF-Token": old_token})
        self.assert_rejected(response)
        unicode_token = client.post("/api/books", json={
            "title": "Untrusted", "author": "Author", "total_copies": 1,
        }, headers={"X-CSRF-Token": "अमान्य-token"})
        self.assertEqual(unicode_token.status_code, 403)
        self.assertTrue(unicode_token.get_json().get("error"))
        self.assertEqual(self.books(), [])

    def test_admin_routes_and_other_students_loans_are_protected(self):
        first, client = self.create_student("first")
        second, other = self.create_student("second")
        book = self.create_book()
        loan = self.issue(client, book["id"])
        for path in ("/api/users", "/api/backup"):
            self.assertEqual(client.get(path).status_code, 403)
        for method, path, payload in (
            ("POST", "/api/books", {"title": "Forbidden", "author": "Author", "total_copies": 1}),
            ("PUT", "/api/settings", {"loan_days": 1, "max_loans": 1, "fine_per_day": 0}),
            ("POST", f"/api/loans/{loan['id']}/pay", {}),
            ("POST", f"/api/loans/{loan['id']}/return", {}),
        ):
            actor = other if path.endswith("/return") else client
            expected = (403, 404) if path.endswith("/return") else (403,)
            self.assertIn(self.mutate(actor, method, path, payload).status_code, expected)
        self.assertEqual(self.loans(other), [])
        self.assertEqual([item["id"] for item in self.loans(client)], [loan["id"]])
        self.assertEqual(len(self.loans()), 1)
        spoof = self.mutate(other, "POST", "/api/loans", {
            "book_id": book["id"], "user_id": first["id"],
        })
        self.assert_rejected(spoof)

    def test_issue_return_inventory_duplicate_and_exhausted_copies(self):
        book = self.create_book(copies=2)
        _, first = self.create_student("first")
        _, second = self.create_student("second")
        _, third = self.create_student("third")
        loan = self.issue(first, book["id"])
        self.assertEqual(self.books()[0]["available_copies"], 1)
        self.assert_rejected(self.mutate(first, "POST", "/api/loans", {"book_id": book["id"]}))
        self.issue(second, book["id"])
        self.assertEqual(self.books()[0]["available_copies"], 0)
        self.assertEqual(self.books(query="?status=available"), [])
        self.assertEqual(len(self.books(query="?status=issued")), 1)
        self.assert_rejected(self.mutate(third, "POST", "/api/loans", {"book_id": book["id"]}))
        self.return_loan(first, loan["id"])
        self.assertEqual(self.books()[0]["available_copies"], 1)
        self.assert_rejected(self.mutate(first, "POST", f"/api/loans/{loan['id']}/return", {}))
        self.issue(first, book["id"])
        self.assertEqual(self.books()[0]["available_copies"], 0)

    def test_due_date_and_borrowing_limit(self):
        self.settings(loan_days=7, max_loans=1)
        _, client = self.create_student()
        first = self.create_book("First")
        second = self.create_book("Second")
        loan = self.issue(client, first["id"])
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
        self.assertEqual(date.fromisoformat(loan["due_date"]), today + timedelta(days=7))
        self.assert_rejected(self.mutate(client, "POST", "/api/loans", {"book_id": second["id"]}))
        self.return_loan(client, loan["id"])
        self.issue(client, second["id"])

    def test_simultaneous_issue_cannot_overbook_last_copy(self):
        book = self.create_book(copies=1)
        _, first = self.create_student("first")
        _, second = self.create_student("second")
        first_token, second_token = self.token(first), self.token(second)
        ready = Barrier(2)

        def borrow(client, token):
            ready.wait(timeout=10)
            return client.post("/api/loans", json={"book_id": book["id"]},
                               headers={"X-CSRF-Token": token})

        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(borrow, first, first_token),
                       executor.submit(borrow, second, second_token)]
            responses = [future.result(timeout=20) for future in pending]
        self.assertEqual(sum(response.status_code == 201 for response in responses), 1)
        for response in responses:
            if response.status_code != 201:
                self.assert_rejected(response)
        self.assertEqual(self.books()[0]["available_copies"], 0)
        self.assertEqual(len(self.loans(query="?status=active")), 1)

    def test_students_cannot_change_due_dates(self):
        book = self.create_book()
        _, client = self.create_student()
        response = self.mutate(client, "POST", "/api/loans", {
            "book_id": book["id"], "due_date": "2099-12-31",
        })
        if response.status_code == 201:
            self.assertNotEqual(response.get_json()["loan"]["due_date"], "2099-12-31")
        else:
            self.assert_rejected(response)

    def test_overdue_fine_uses_calendar_days_and_issue_time_rate(self):
        self.settings(fine_per_day=2.5)
        book = self.create_book()
        _, client = self.create_student()
        loan = self.issue(client, book["id"])
        today = self.make_overdue(loan["id"], 3)
        self.settings(fine_per_day=20)
        active = self.loans(client, "?status=overdue")[0]
        self.assertEqual(active["days_overdue"], 3)
        self.assertAlmostEqual(active["fine_amount"], 7.5)
        returned = self.return_loan(client, loan["id"])
        self.assertAlmostEqual(returned["fine_amount"], 7.5)
        self.settings(fine_per_day=40)
        with patch("app.library_today", return_value=today + timedelta(days=10)):
            returned = self.loans(client, "?status=returned")[0]
        self.assertAlmostEqual(returned["fine_amount"], 7.5)
        settled = self.mutate(self.admin, "POST", f"/api/loans/{loan['id']}/pay", {})
        self.assertEqual(settled.status_code, 200, settled.get_json())
        self.assertAlmostEqual(settled.get_json()["loan"]["paid_amount"], 7.5)
        self.assertEqual(settled.get_json()["loan"]["balance"], 0)

    def test_return_on_due_date_has_no_fine_and_active_fine_cannot_be_settled(self):
        book = self.create_book()
        _, client = self.create_student()
        loan = self.issue(client, book["id"])
        self.make_overdue(loan["id"], 0)
        response = self.mutate(self.admin, "POST", f"/api/loans/{loan['id']}/pay", {})
        self.assert_rejected(response)
        returned = self.return_loan(client, loan["id"])
        self.assertEqual(returned["fine_amount"], 0)

    def test_book_search_update_copy_floor_and_archive_guard(self):
        book = self.create_book("Clean Code", "Robert Martin", copies=2)
        self.create_book("Python Basics", "Jane Doe")
        self.assertEqual([b["id"] for b in self.books(query="?q=martin")], [book["id"]])
        self.assertEqual([b["id"] for b in self.books(query="?q=CLEAN")], [book["id"]])
        _, first = self.create_student("first")
        _, second = self.create_student("second")
        one = self.issue(first, book["id"])
        two = self.issue(second, book["id"])
        payload = {"title": "Clean Code Updated", "author": "Robert Martin", "total_copies": 1}
        self.assert_rejected(self.mutate(self.admin, "PUT", f"/api/books/{book['id']}", payload))
        self.assert_rejected(self.mutate(self.admin, "DELETE", f"/api/books/{book['id']}"))
        self.return_loan(first, one["id"])
        response = self.mutate(self.admin, "PUT", f"/api/books/{book['id']}", payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["book"]["available_copies"], 0)
        self.return_loan(second, two["id"])
        response = self.mutate(self.admin, "DELETE", f"/api/books/{book['id']}")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertNotIn(book["id"], [b["id"] for b in self.books()])
        self.assertEqual(len(self.loans(query="?status=returned")), 2)

    def test_user_update_deactivation_invalidates_existing_session(self):
        student, client = self.create_student()
        payload = {
            "name": "Updated Student", "email": "updated@example.test",
            "student_id": "UPDATED", "department": "Math", "active": True,
        }
        response = self.mutate(self.admin, "PUT", f"/api/users/{student['id']}", payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        users = self.admin.get("/api/users?q=UPDATED").get_json()["users"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["name"], "Updated Student")
        self.login(client, "updated@example.test", "Student@123")
        response = self.mutate(self.admin, "DELETE", f"/api/users/{student['id']}")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIsNone(client.get("/api/session").get_json()["user"])
        response = self.mutate(self.app.test_client(), "POST", "/api/login", {
            "email": "updated@example.test", "password": "Student@123",
        })
        self.assertEqual(response.status_code, 401)
        admin_id = self.admin.get("/api/session").get_json()["user"]["id"]
        self.assert_rejected(self.mutate(self.admin, "DELETE", f"/api/users/{admin_id}"))

    def test_profile_password_change_requires_current_password(self):
        _, client = self.create_student()
        response = self.mutate(client, "PUT", "/api/profile", {
            "current_password": "wrong", "new_password": "Changed@123",
        })
        self.assert_rejected(response, (400, 401, 403, 422))
        response = self.mutate(client, "PUT", "/api/profile", {
            "name": "Profile Student", "department": "Physics",
            "current_password": "Student@123", "new_password": "Changed@123",
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.login(self.app.test_client(), "one@example.test", "Changed@123")
        old_password = self.mutate(self.app.test_client(), "POST", "/api/login", {
            "email": "one@example.test", "password": "Student@123",
        })
        self.assertEqual(old_password.status_code, 401)

    def test_settings_validation_is_atomic(self):
        original = self.admin.get("/api/settings").get_json()["settings"]
        for changed in (
            {"loan_days": 0}, {"max_loans": -1}, {"fine_per_day": -0.1},
            {"loan_days": 1.5}, {"fine_per_day": "not a number"},
        ):
            payload = {**original, **changed}
            response = self.mutate(self.admin, "PUT", "/api/settings", payload)
            self.assert_rejected(response)
            self.assertEqual(self.admin.get("/api/settings").get_json()["settings"], original)

    def test_backup_restore_recovers_data_and_invalidates_all_sessions(self):
        kept = self.create_book("Backed Up Book")
        _, student = self.create_student()
        original_loan = self.issue(student, kept["id"])
        backup = self.admin.get("/api/backup")
        self.assertEqual(backup.status_code, 200)
        self.assertTrue(backup.data.startswith(b"SQLite format 3\x00"))
        self.assertIn("attachment", backup.headers.get("Content-Disposition", ""))
        added = self.create_book("Created After Backup")
        self.return_loan(student, original_loan["id"])
        self.issue(student, added["id"])
        csrf = self.token(self.admin)
        response = self.admin.post("/api/restore", data={
            "file": (BytesIO(backup.data), "smartlib-backup.db"),
            "confirmation": "RESTORE", "password": "Admin@12345",
        }, headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json().get("csrf_token"))
        self.assertIsNone(self.admin.get("/api/session").get_json()["user"])
        self.assertIsNone(student.get("/api/session").get_json()["user"])
        self.login(self.admin, "admin@smartlib.local", "Admin@12345")
        ids = [book["id"] for book in self.books()]
        self.assertIn(kept["id"], ids)
        self.assertNotIn(added["id"], ids)
        restored_book = next(book for book in self.books() if book["id"] == kept["id"])
        self.assertEqual(restored_book["available_copies"], 1)
        self.assertEqual([loan["id"] for loan in self.loans(query="?status=active")], [original_loan["id"]])

    def test_backup_download_rejects_file_larger_than_restore_limit(self):
        book = self.create_book()
        self.app.config["BACKUP_MAX_BYTES"] = 512
        response = self.admin.get("/api/backup")
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertTrue(response.get_json().get("error"))
        self.assertNotIn("attachment", response.headers.get("Content-Disposition", ""))
        self.assertEqual([item["id"] for item in self.books()], [book["id"]])

    def test_restore_rejects_corrupt_data_wrong_password_and_confirmation(self):
        book = self.create_book()
        backup = self.admin.get("/api/backup").data
        foreign_db = Path(self.temp.name) / "foreign.db"
        with closing(sqlite3.connect(foreign_db)) as conn, conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
        no_admin_db = Path(self.temp.name) / "no-admin.db"
        no_admin_db.write_bytes(backup)
        with closing(sqlite3.connect(no_admin_db)) as conn, conn:
            conn.execute("UPDATE users SET active = 0 WHERE role = 'admin'")
        triggered_db = Path(self.temp.name) / "extra-trigger.db"
        triggered_db.write_bytes(backup)
        with closing(sqlite3.connect(triggered_db)) as conn, conn:
            conn.execute("CREATE TRIGGER unexpected_trigger AFTER INSERT ON books "
                         "BEGIN DELETE FROM books WHERE id = NEW.id; END")
        bad_hash_db = Path(self.temp.name) / "malformed-admin-password.db"
        bad_hash_db.write_bytes(backup)
        with closing(sqlite3.connect(bad_hash_db)) as conn, conn:
            conn.execute("UPDATE users SET password_hash = 'scrypt:broken' WHERE role = 'admin'")
        scenarios = (
            (b"This is not a database", "RESTORE", "Admin@12345"),
            (foreign_db.read_bytes(), "RESTORE", "Admin@12345"),
            (no_admin_db.read_bytes(), "RESTORE", "Admin@12345"),
            (triggered_db.read_bytes(), "RESTORE", "Admin@12345"),
            (bad_hash_db.read_bytes(), "RESTORE", "Admin@12345"),
            (backup, "RESTORE", "WrongPassword"),
            (backup, "restore", "Admin@12345"),
        )
        for contents, confirmation, password in scenarios:
            response = self.admin.post("/api/restore", data={
                "file": (BytesIO(contents), "backup.db"),
                "confirmation": confirmation, "password": password,
            }, headers={"X-CSRF-Token": self.token(self.admin)}, content_type="multipart/form-data")
            self.assert_rejected(response, (400, 401, 403, 409, 422))
            self.assertEqual([item["id"] for item in self.books()], [book["id"]])
            self.assertIsNotNone(self.admin.get("/api/session").get_json()["user"])


if __name__ == "__main__":
    unittest.main()
