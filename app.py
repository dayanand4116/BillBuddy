
import os
import re
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path
from math import isfinite

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key-before-production",
)

# Vercel serverless functions have writable temporary storage at /tmp.
DATA_DIR = "/tmp"
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DB_PATH = os.environ.get(
    "DATABASE_PATH",
    os.path.join(DATA_DIR, "billbuddy.db"),
)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL,
                due_date TEXT NOT NULL,
                recurring TEXT NOT NULL DEFAULT 'No',
                status TEXT NOT NULL DEFAULT 'Pending',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def extract_bill_text(path):
    """
    Extract text from an uploaded image using Tesseract OCR.

    Requires:
      - Pillow
      - pytesseract
      - The Tesseract OCR engine installed on the machine.
    """
    result = {
        "text": "",
        "amount": "",
        "due_date": "",
        "error": "",
    }

    try:
        import pytesseract
        from PIL import Image, ImageOps, ImageEnhance

        # Allow an explicitly configured Tesseract executable path.
        # On Vercel, setting this variable alone does not install Tesseract.
        tesseract_cmd = os.environ.get("TESSERACT_CMD")
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

        with Image.open(path) as original:
            original.verify()

        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")

            # Improve contrast for some scanned or photographed bills.
            image = ImageOps.grayscale(image)
            image = ImageEnhance.Contrast(image).enhance(1.5)

            text = pytesseract.image_to_string(
                image,
                config="--psm 6",
            ).strip()

        app.logger.info("OCR completed; extracted %s characters", len(text))

        if not text:
            result["error"] = (
                "No readable text was found. Try a clearer, well-lit "
                "image or enter the bill details manually."
            )
            return result

        # Find common Indian currency formats, for example:
        # ₹1,250.00, Rs. 1250, INR 1250
        amount_patterns = [
            r"(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
            r"(?:total|amount\s*due|grand\s*total|bill\s*amount)"
            r"\s*[:\-]?\s*(?:₹|Rs\.?|INR)?\s*"
            r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        ]

        amount = ""
        for pattern in amount_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                amount = match.group(1).replace(",", "")
                break

        # Recognize common year-first and day-first date formats.
        date_patterns = [
            r"\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\b",
            r"\b(\d{1,2}[-/]\d{1,2}[-/]20\d{2})\b",
        ]

        due_date = ""
        for pattern in date_patterns:
            match = re.search(pattern, text)
            if not match:
                continue

            raw_date = match.group(1).replace("/", "-")
            parsed_date = None

            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y"):
                try:
                    parsed_date = datetime.strptime(
                        raw_date, fmt
                    ).date()
                    break
                except ValueError:
                    continue

            if parsed_date:
                due_date = parsed_date.isoformat()
                break

        result.update({
            "text": text[:4000],
            "amount": amount,
            "due_date": due_date,
        })

        return result

    except ImportError:
        app.logger.exception("OCR dependency is missing")
        result["error"] = (
            "An OCR Python package is missing. Install Pillow and "
            "pytesseract, then restart the application."
        )
        return result

    except Exception as exc:
        app.logger.exception("OCR processing failed")

        error_name = type(exc).__name__
        error_message = str(exc)

        if (
            error_name == "TesseractNotFoundError"
            or "tesseract is not installed" in error_message.lower()
        ):
            result["error"] = (
                "The Tesseract OCR engine is not installed or cannot "
                "be found. Installing pytesseract alone is not enough. "
                "On Vercel, configure a compatible OCR engine or use "
                "an external OCR API."
            )
        else:
            result["error"] = (
                f"Could not scan this image ({error_name}). "
                "Check the Flask terminal logs for details."
            )

        return result


@app.route("/")
def dashboard():
    today = date.today().isoformat()

    with get_db() as db:
        bills = db.execute(
            "SELECT * FROM bills ORDER BY due_date ASC"
        ).fetchall()

        total = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM bills"
        ).fetchone()[0]

        pending = db.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM bills
            WHERE status = 'Pending'
        """).fetchone()[0]

        paid_count = db.execute("""
            SELECT COUNT(*) FROM bills WHERE status = 'Paid'
        """).fetchone()[0]

        upcoming = db.execute("""
            SELECT COUNT(*) FROM bills
            WHERE status = 'Pending'
              AND due_date >= ?
              AND due_date <= date(?, '+7 days')
        """, (today, today)).fetchone()[0]

    return render_template(
        "index.html",
        bills=bills,
        total=total,
        pending=pending,
        paid_count=paid_count,
        upcoming=upcoming,
        today=today,
    )


@app.route("/add", methods=["GET", "POST"])
def add_bill():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "Other")
        amount_text = request.form.get("amount", "0").strip()
        due_date = request.form.get("due_date", "")
        recurring = request.form.get("recurring", "No")
        notes = request.form.get("notes", "").strip()

        if not title or not due_date:
            flash("Please enter a bill name and due date.", "danger")
            return render_template(
                "add.html", today=date.today().isoformat()
            )

        try:
            amount = float(amount_text)
            if not isfinite(amount) or amount < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash(
                "Amount must be a valid non-negative number.",
                "danger",
            )
            return render_template(
                "add.html", today=date.today().isoformat()
            )

        with get_db() as db:
            db.execute("""
                INSERT INTO bills
                (title, category, amount, due_date,
                 recurring, status, notes)
                VALUES (?, ?, ?, ?, ?, 'Pending', ?)
            """, (
                title, category, amount, due_date,
                recurring, notes,
            ))

        flash("Bill added successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template(
        "add.html", today=date.today().isoformat()
    )


@app.route("/toggle/<int:bill_id>", methods=["POST"])
def toggle_paid(bill_id):
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM bills WHERE id = ?",
            (bill_id,),
        ).fetchone()

        if row:
            new_status = (
                "Paid" if row["status"] == "Pending" else "Pending"
            )
            db.execute(
                "UPDATE bills SET status = ? WHERE id = ?",
                (new_status, bill_id),
            )
            flash(f"Bill marked {new_status.lower()}.", "success")

    return redirect(url_for("dashboard"))


@app.route("/delete/<int:bill_id>", methods=["POST"])
def delete_bill(bill_id):
    with get_db() as db:
        db.execute(
            "DELETE FROM bills WHERE id = ?",
            (bill_id,),
        )

    flash("Bill deleted.", "info")
    return redirect(url_for("dashboard"))


@app.route("/ocr", methods=["GET", "POST"])
def ocr():
    result = None

    if request.method == "POST":
        # Support either input name so HTML/backend stay compatible.
        file = (
            request.files.get("file")
            or request.files.get("bill_image")
        )

        if not file or not file.filename:
            flash("Choose a bill image first.", "danger")

        elif not allowed_file(file.filename):
            flash(
                "Upload PNG, JPG, JPEG, or WEBP only.",
                "danger",
            )

        else:
            filename = secure_filename(file.filename)
            suffix = Path(filename).suffix.lower()
            path = None

            try:
                # Save to a unique temporary filename to avoid collisions.
                with tempfile.NamedTemporaryFile(
                    prefix="bill_",
                    suffix=suffix,
                    dir=UPLOAD_DIR,
                    delete=False,
                ) as temp_file:
                    path = temp_file.name

                file.save(path)

                if os.path.getsize(path) > MAX_UPLOAD_BYTES:
                    result = {
                        "filename": filename,
                        "text": "",
                        "amount": "",
                        "due_date": "",
                        "error": "Image is too large. Maximum size is 10 MB.",
                    }
                else:
                    result = extract_bill_text(path)
                    result["filename"] = filename

                if result.get("error"):
                    flash(result["error"], "warning")

            except Exception:
                app.logger.exception("Upload processing failed")
                result = {
                    "filename": filename,
                    "text": "",
                    "amount": "",
                    "due_date": "",
                    "error": "Could not process the uploaded image.",
                }
                flash(result["error"], "danger")

            finally:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        app.logger.warning(
                            "Could not remove temporary image: %s", path
                        )

    return render_template("ocr.html", result=result)


@app.route("/subscriptions")
def subscriptions():
    with get_db() as db:
        rows = db.execute("""
            SELECT * FROM bills
            WHERE recurring = 'Yes'
            ORDER BY due_date
        """).fetchall()

    return render_template("subscriptions.html", bills=rows)


# Initialize the database table when the app starts.
try:
    init_db()
except Exception:
    app.logger.exception("Database initialization failed")


if __name__ == "__main__":
    app.run(debug=True)
