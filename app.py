
import os
import io
import json
import re
import sqlite3
import mimetypes
from functools import wraps
from datetime import datetime, date, timedelta
from math import isfinite

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from google import genai
from google.genai import types
from PIL import Image, ImageOps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32).hex()

DATABASE = "/tmp/billbuddy.db"
UPLOAD_FOLDER = "/tmp/uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ---------------- DATABASE ----------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA foreign_keys = ON")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            amount REAL DEFAULT 0,
            due_date TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            category TEXT DEFAULT 'Other',
            status TEXT DEFAULT 'Pending',
            recurring TEXT DEFAULT 'No',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            amount REAL DEFAULT 0,
            billing_cycle TEXT DEFAULT 'monthly',
            next_due_date TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)

    # Add columns to an existing database without deleting user data.
    columns = {
        row[1]
        for row in db.execute("PRAGMA table_info(bills)").fetchall()
    }

    migrations = {
        "category": "ALTER TABLE bills ADD COLUMN category TEXT DEFAULT 'Other'",
        "status": "ALTER TABLE bills ADD COLUMN status TEXT DEFAULT 'Pending'",
        "recurring": "ALTER TABLE bills ADD COLUMN recurring TEXT DEFAULT 'No'",
    }

    for column, statement in migrations.items():
        if column not in columns:
            db.execute(statement)

    db.execute("""
        UPDATE bills
        SET category = 'Other'
        WHERE category IS NULL OR category = ''
    """)
    db.execute("""
        UPDATE bills
        SET status = 'Pending'
        WHERE status IS NULL OR status = ''
    """)
    db.execute("""
        UPDATE bills
        SET recurring = 'No'
        WHERE recurring IS NULL OR recurring = ''
    """)

    db.commit()
    db.close()


init_db()


# ---------------- AUTHENTICATION ----------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
def home():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("register.html")

        db = get_db()

        try:
            cursor = db.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password))
            )
            db.commit()

            session.clear()
            session["user_id"] = cursor.lastrowid
            session["username"] = username

            flash("Account created successfully.", "success")
            return redirect(url_for("dashboard"))

        except sqlite3.IntegrityError:
            flash("That username is already taken.", "error")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = get_db().execute(
            """
            SELECT id, username, password_hash
            FROM users
            WHERE username = ?
            """,
            (username,)
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("dashboard"))

        flash("Invalid username or password.", "error")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ---------------- DASHBOARD ----------------

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user_id = session["user_id"]

    bills = db.execute(
        """
        SELECT *
        FROM bills
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,)
    ).fetchall()

    subscriptions = db.execute(
        """
        SELECT *
        FROM subscriptions
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,)
    ).fetchall()

    total = sum(float(b["amount"] or 0) for b in bills)

    pending = sum(
        float(b["amount"] or 0)
        for b in bills
        if (b["status"] or "Pending") != "Paid"
    )

    paid_count = sum(
        1 for b in bills if (b["status"] or "Pending") == "Paid"
    )

    today = date.today()
    next_week = today + timedelta(days=7)
    upcoming = 0

    for bill in bills:
        if (bill["status"] or "Pending") == "Paid":
            continue

        due_date = (bill["due_date"] or "").strip()
        if not due_date:
            continue

        try:
            due = date.fromisoformat(due_date)
            if today <= due <= next_week:
                upcoming += 1
        except ValueError:
            continue

    return render_template(
        "index.html",
        bills=bills,
        subscriptions=subscriptions,
        total=total,
        pending=pending,
        paid_count=paid_count,
        upcoming=upcoming,
        username=session.get("username")
    )


# ---------------- ADD BILL ----------------

@app.route("/add", methods=["GET", "POST"])
@login_required
def add_bill():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        amount_text = request.form.get("amount", "0").strip()
        due_date = request.form.get("due_date", "").strip()
        notes = request.form.get("notes", "").strip()
        category = request.form.get("category", "Other").strip() or "Other"
        recurring = request.form.get("recurring", "No").strip()

        if not title:
            flash("Please enter a bill title.", "error")
            return render_template("add.html")

        if recurring not in {"Yes", "No"}:
            recurring = "No"

        try:
            amount = float(amount_text or 0)
            if not isfinite(amount) or amount < 0:
                raise ValueError
        except ValueError:
            flash("Enter a valid non-negative amount.", "error")
            return render_template("add.html")

        if due_date:
            try:
                date.fromisoformat(due_date)
            except ValueError:
                flash("Enter a valid due date.", "error")
                return render_template("add.html")

        db = get_db()
        db.execute(
            """
            INSERT INTO bills
                (user_id, title, amount, due_date, notes,
                 category, status, recurring)
            VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?)
            """,
            (
                session["user_id"], title, amount, due_date,
                notes, category, recurring
            )
        )
        db.commit()

        flash("Bill added successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template("add.html")


# ---------------- MARK PAID / UNDO ----------------

@app.route("/toggle-paid/<int:bill_id>", methods=["POST"])
@login_required
def toggle_paid(bill_id):
    db = get_db()
    user_id = session["user_id"]

    bill = db.execute(
        "SELECT id, status FROM bills WHERE id = ? AND user_id = ?",
        (bill_id, user_id)
    ).fetchone()

    if bill is None:
        flash("Bill not found.", "error")
        return redirect(url_for("dashboard"))

    new_status = "Pending" if bill["status"] == "Paid" else "Paid"

    db.execute(
        "UPDATE bills SET status = ? WHERE id = ? AND user_id = ?",
        (new_status, bill_id, user_id)
    )
    db.commit()

    flash("Bill status updated to " + new_status + ".", "success")
    return redirect(url_for("dashboard"))


# ---------------- DELETE BILL ----------------

@app.route("/delete-bill/<int:bill_id>", methods=["POST"])
@login_required
def delete_bill(bill_id):
    db = get_db()
    db.execute(
        "DELETE FROM bills WHERE id = ? AND user_id = ?",
        (bill_id, session["user_id"])
    )
    db.commit()

    flash("Bill deleted if it belonged to your account.", "success")
    return redirect(url_for("dashboard"))


# ---------------- SUBSCRIPTIONS ----------------

@app.route("/subscriptions", methods=["GET", "POST"])
@login_required
def subscriptions_page():
    db = get_db()
    user_id = session["user_id"]

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        amount_text = request.form.get("amount", "0").strip()
        billing_cycle = request.form.get("billing_cycle", "monthly").strip()
        next_due_date = request.form.get("next_due_date", "").strip()

        if not name:
            flash("Please enter a subscription name.", "error")
            return redirect(url_for("subscriptions_page"))

        if billing_cycle not in {"weekly", "monthly", "yearly"}:
            billing_cycle = "monthly"

        try:
            amount = float(amount_text or 0)
            if not isfinite(amount) or amount < 0:
                raise ValueError
        except ValueError:
            flash("Enter a valid non-negative amount.", "error")
            return redirect(url_for("subscriptions_page"))

        if next_due_date:
            try:
                date.fromisoformat(next_due_date)
            except ValueError:
                flash("Enter a valid subscription due date.", "error")
                return redirect(url_for("subscriptions_page"))

        db.execute(
            """
            INSERT INTO subscriptions
                (user_id, name, amount, billing_cycle, next_due_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, name, amount, billing_cycle, next_due_date)
        )
        db.commit()

        flash("Subscription added successfully.", "success")
        return redirect(url_for("subscriptions_page"))

    items = db.execute(
        """
        SELECT *
        FROM subscriptions
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,)
    ).fetchall()

    return render_template("subscriptions.html", subscriptions=items)


@app.route("/delete-subscription/<int:subscription_id>", methods=["POST"])
@login_required
def delete_subscription(subscription_id):
    db = get_db()
    db.execute(
        """
        DELETE FROM subscriptions
        WHERE id = ? AND user_id = ?
        """,
        (subscription_id, session["user_id"])
    )
    db.commit()

    flash("Subscription deleted if it belonged to your account.", "success")
    return redirect(url_for("subscriptions_page"))


# ---------------- BILL IMAGE SCANNING ----------------

def extract_bill_text(path):
    result = {
        "text": "",
        "amount": "",
        "due_date": "",
        "error": ""
    }

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        result["error"] = (
            "GEMINI_API_KEY is missing from environment variables."
        )
        return result

    try:
        mime_type, _ = mimetypes.guess_type(path)

        if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            result["error"] = "Upload a PNG, JPG, JPEG or WEBP image."
            return result

        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original)
            image.load()
            image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)

            if image.mode != "RGB":
                image = image.convert("RGB")

            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88, optimize=True)
            image_bytes = buffer.getvalue()

        client = genai.Client(api_key=api_key)

        prompt = """
Read this bill or invoice. Return ONLY a valid JSON object:
{
  "text": "readable bill text",
  "amount": "final amount payable as a number, or empty string",
  "due_date": "due date in YYYY-MM-DD format, or empty string"
}
Use the final total, not subtotal. Extract the due date, not issue date.
Do not invent missing information. Return JSON only, without Markdown.
"""

        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=[
                prompt,
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg"
                )
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0
            )
        )

        if not response.text:
            raise ValueError("Gemini returned an empty response.")

        data = json.loads(response.text)

        text = str(data.get("text") or "").strip()[:4000]
        amount_raw = str(data.get("amount") or "").strip()
        date_raw = str(data.get("due_date") or "").strip()

        amount = ""
        if amount_raw:
            cleaned = re.sub(r"[₹,\s]", "", amount_raw)
            cleaned = re.sub(r"^(?:INR|Rs\.?)", "", cleaned, flags=re.I)

            try:
                value = float(cleaned)
                if isfinite(value) and value >= 0:
                    amount = str(value)
            except ValueError:
                pass

        due_date = ""
        if date_raw:
            try:
                due_date = datetime.strptime(
                    date_raw, "%Y-%m-%d"
                ).date().isoformat()
            except ValueError:
                pass

        result.update(text=text, amount=amount, due_date=due_date)

        if not text:
            result["error"] = (
                "No readable text detected. Try a clearer image."
            )

        return result

    except Exception:
        app.logger.exception("Gemini bill scanning failed")
        result["error"] = (
            "Bill scanning failed. Check your Gemini API key, model access "
            "and Vercel function logs."
        )
        return result


@app.route("/ocr", methods=["GET", "POST"])
@login_required
def ocr():
    result = {
        "text": "",
        "amount": "",
        "due_date": "",
        "error": ""
    }

    if request.method == "POST":
        upload = request.files.get("file")

        if not upload or not upload.filename:
            result["error"] = "Choose a bill image first."
            return render_template("ocr.html", result=result)

        filename = secure_filename(upload.filename)
        extension = (
            filename.rsplit(".", 1)[-1].lower()
            if "." in filename else ""
        )

        if extension not in ALLOWED_EXTENSIONS:
            result["error"] = "Upload a PNG, JPG, JPEG or WEBP image."
            return render_template("ocr.html", result=result)

        path = os.path.join(UPLOAD_FOLDER, filename)

        try:
            upload.save(path)
            result = extract_bill_text(path)
        finally:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                app.logger.warning("Could not remove temporary upload.")

    return render_template("ocr.html", result=result)


# ---------------- LOCAL DEVELOPMENT ----------------

if __name__ == "__main__":
    app.run(debug=True)
