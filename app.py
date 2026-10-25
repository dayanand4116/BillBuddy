from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3, os, re
from datetime import date, datetime
from werkzeug.utils import secure_filename

import pytesseract

pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

app = Flask(__name__)
app.secret_key = "change-this-secret-key"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "billbuddy.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            due_date TEXT NOT NULL,
            recurring TEXT NOT NULL DEFAULT 'No',
            status TEXT NOT NULL DEFAULT 'Pending',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_bill_text(path):
    """Optional OCR. Requires Tesseract installed plus pytesseract and Pillow."""
    try:
        import pytesseract
        from PIL import Image
        text = pytesseract.image_to_string(Image.open(path))
        amount_match = re.search(r'(?:₹|Rs\.?|INR)\s*([0-9,]+(?:\.[0-9]{1,2})?)', text, re.I)
        date_match = re.search(r'\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\b', text)
        amount = amount_match.group(1).replace(",", "") if amount_match else ""
        due_date = ""
        if date_match:
            raw = date_match.group(1).replace("/", "-")
            try:
                due_date = datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
            except ValueError:
                pass
        return {"text": text[:4000], "amount": amount, "due_date": due_date, "error": ""}
    except ImportError:
        return {"text": "", "amount": "", "due_date": "", "error": "OCR packages are not installed. Install requirements and Tesseract, or enter details manually."}
    except Exception as exc:
        return {"text": "", "amount": "", "due_date": "", "error": f"OCR could not read this image: {exc}"}

@app.route("/")
def dashboard():
    today = date.today().isoformat()
    with get_db() as db:
        bills = db.execute("SELECT * FROM bills ORDER BY due_date ASC").fetchall()
        total = db.execute("SELECT COALESCE(SUM(amount),0) FROM bills").fetchone()[0]
        pending = db.execute("SELECT COALESCE(SUM(amount),0) FROM bills WHERE status='Pending'").fetchone()[0]
        paid_count = db.execute("SELECT COUNT(*) FROM bills WHERE status='Paid'").fetchone()[0]
        upcoming = db.execute("SELECT COUNT(*) FROM bills WHERE status='Pending' AND due_date>=? AND due_date<=date(?, '+7 days')", (today, today)).fetchone()[0]
    return render_template("index.html", bills=bills, total=total, pending=pending, paid_count=paid_count, upcoming=upcoming, today=today)

@app.route("/add", methods=["GET", "POST"])
def add_bill():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "Other")
        amount = request.form.get("amount", "0").strip()
        due_date = request.form.get("due_date", "")
        recurring = request.form.get("recurring", "No")
        notes = request.form.get("notes", "").strip()
        if not title or not due_date:
            flash("Please enter a bill name and due date.", "danger")
            return render_template("add.html", today=date.today().isoformat())
        try:
            amount = float(amount)
            if amount < 0: raise ValueError()
        except ValueError:
            flash("Amount must be a valid non-negative number.", "danger")
            return render_template("add.html", today=date.today().isoformat())
        with get_db() as db:
            db.execute("INSERT INTO bills(title,category,amount,due_date,recurring,status,notes) VALUES(?,?,?,?,?,'Pending',?)",
                       (title, category, amount, due_date, recurring, notes))
        flash("Bill added successfully.", "success")
        return redirect(url_for("dashboard"))
    return render_template("add.html", today=date.today().isoformat())

@app.route("/toggle/<int:bill_id>", methods=["POST"])
def toggle_paid(bill_id):
    with get_db() as db:
        row = db.execute("SELECT status FROM bills WHERE id=?", (bill_id,)).fetchone()
        if row:
            new_status = "Paid" if row["status"] == "Pending" else "Pending"
            db.execute("UPDATE bills SET status=? WHERE id=?", (new_status, bill_id))
            flash(f"Bill marked {new_status.lower()}.", "success")
    return redirect(url_for("dashboard"))

@app.route("/delete/<int:bill_id>", methods=["POST"])
def delete_bill(bill_id):
    with get_db() as db:
        db.execute("DELETE FROM bills WHERE id=?", (bill_id,))
    flash("Bill deleted.", "info")
    return redirect(url_for("dashboard"))

@app.route("/ocr", methods=["GET", "POST"])
def ocr():
    result = None
    if request.method == "POST":
        file = request.files.get("bill_image")
        if not file or not file.filename:
            flash("Choose a bill image first.", "danger")
        elif not allowed_file(file.filename):
            flash("Upload PNG, JPG, JPEG, or WEBP only.", "danger")
        else:
            filename = secure_filename(file.filename)
            path = os.path.join(UPLOAD_DIR, filename)
            file.save(path)
            result = extract_bill_text(path)
            result["filename"] = filename
            if result.get("error"):
                flash(result["error"], "warning")
            elif not result.get("text"):
                flash("No readable text found. Try a clearer image or enter details manually.", "warning")
    return render_template("ocr.html", result=result)

@app.route("/subscriptions")
def subscriptions():
    with get_db() as db:
        rows = db.execute("SELECT * FROM bills WHERE recurring='Yes' ORDER BY due_date").fetchall()
    return render_template("subscriptions.html", bills=rows)

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
