# BillBuddy — Smart Bill & Subscription Manager

A student mini-project web prototype built with Python Flask, SQLite, Bootstrap and optional OCR.

## Features
- Dashboard with total listed amount, pending amount, paid count, and bills due in the next 7 days
- Add bills with category, amount, due date, recurring flag and notes
- Mark bills paid/unpaid and delete entries
- Recurring subscription list
- Optional image OCR attempt for bill text, amount and date (manual verification required)

## Run locally
1. Install Python 3.10+.
2. Open a terminal in this folder.
3. Create and activate a virtual environment (optional but recommended).
4. Install dependencies: `pip install -r requirements.txt`
5. For OCR, install the Tesseract OCR system application separately and ensure it is on PATH.
6. Run: `python app.py`
7. Open `http://127.0.0.1:5000`

The SQLite database `billbuddy.db` is created automatically. Uploaded images are stored in `uploads/`.
This is an educational prototype, not a payment processor. It does not connect to bank accounts or make payments.
OCR is optional and may misread details; always verify extracted values against the original bill.
For a real deployment, turn off Flask debug mode, use a strong secret key, add authentication/CSRF protection, and restrict file sizes.
