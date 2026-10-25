
import os
import re
import io
import json
import sqlite3
import tempfile
import mimetypes
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
from google import genai
from google.genai import types
from PIL import Image, ImageOps

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key-before-production",
)

# Temporary storage for Vercel serverless functions.
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
    """Resize a bill image and use Gemini to extract its information."""

    result = {
        "text": "",
        "amount": "",
        "due_date": "",
        "error": "",
    }

    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        result["error"] = (
            "Gemini API key is missing. Add GEMINI_API_KEY "
            "in your Vercel project environment variables."
        )
        return result

    try:
        mime_type, _ = mimetypes.guess_type(path)

        if mime_type not in {
            "image/png",
            "image/jpeg",
            "image/webp",
        }:
            result["error"] = (
                "Unsupported image format. Upload PNG, JPG, JPEG or WEBP."
            )
            return result

        # Resize the image to reduce upload size and processing overhead.
        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original)
            image.load()

            image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)

            if image.mode != "RGB":
                image = image.convert("RGB")

            buffer = io.BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=88,
                optimize=True,
            )
            image_bytes = buffer.getvalue()

        client = genai.Client(api_key=api_key)

        prompt = """
You are helping extract information from a bill or invoice.

Read the image and return ONLY a valid JSON object with these keys:
{
  "text": "all readable bill text",
  "amount": "final payable amount as a number, or empty string",
  "due_date": "due date in YYYY-MM-DD format, or empty string"
}

Instructions:
- Extract the final amount payable, total due, or grand total.
- Do not use a subtotal or tax amount if a final total is visible.
- Extract the due date, not the bill issue date.
- If the due date is unclear or absent, return an empty string.
- Never invent missing information.
- Keep the extracted text concise enough to fit in the response.
- Return valid JSON only, without Markdown fences.
"""

        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=[
                prompt,
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg",
                ),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned an empty response.")

        extracted = json.loads(response.text)

        text = str(extracted.get("text") or "").strip()[:4000]
        amount_text = str(extracted.get("amount") or "").strip()
        raw_due_date = str(extracted.get("due_date") or "").strip()

        # Validate the extracted amount.
        amount = ""
        if amount_text:
            cleaned_amount = re.sub(r"[₹,\s]", "", amount_text)
            cleaned_amount = re.sub(
                r"^(?:INR|Rs\.?)",
                "",
                cleaned_amount,
                flags=re.IGNORECASE,
            )

            try:
                parsed_amount = float(cleaned_amount)
                if isfinite(parsed_amount) and parsed_amount >= 0:
                    amount = str(parsed_amount)
            except (ValueError, TypeError):
                amount = ""

        # Validate the extracted due date.
        due_date = ""
        if raw_due_date:
            try:
                due_date = datetime.strptime(
                    raw_due_date, "%Y-%m-%d"
                ).date().isoformat()
            except ValueError:
                due_date = ""

        result.update({
            "text": text,
            "amount": amount,
            "due_date": due_date,
        })

        if not text:
            result["error"] = (
                "No readable text was detected. Try a clearer bill image."
            )

        app.logger.info(
            "Gemini scan completed; extracted %s characters",
            len(text),
        )

        return result

    except Exception:
        app.logger.exception("Gemini bill scanning failed")
        result["error"] = (
            "Gemini could not scan this image. Check your API access "
            "and Vercel logs."
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
                        "error": (
                            "Image is too large. Maximum size is 10 MB."
                        ),
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


try:
    init_db()
except Exception:
    app.logger.exception("Database initialization failed")


if __name__ == "__main__":
    app.run(debug=True)
