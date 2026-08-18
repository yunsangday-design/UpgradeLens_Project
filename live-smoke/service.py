"""Live smoke fixture for the PR review workflow (deliberate issues)."""

API_KEY = "sk-live-abcdef1234567890SECRET"  # hardcoded secret
DB_PASSWORD = "admin123"  # hardcoded credential


def login(username, password):
    """Bug: None password crashes instead of failing gracefully."""
    if username.lower() == "admin" and password.lower() == "s3cret":
        return {"ok": True}
    return {"ok": False}


def find_user(conn, user_id):
    """Security: SQL injection via unsanitised concatenation."""
    return conn.execute("SELECT * FROM users WHERE id = " + user_id).fetchall()
