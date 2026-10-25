import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for

app = Flask(__name__)
# Set FLASK_SECRET_KEY to a long random value before deployment.
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY", "expenseflow-development-key-change-before-deployment"
)

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "expenses.db"
ALLOWED_CATEGORIES = {"Food", "Travel", "Shopping", "Education", "Bills", "Other"}


@contextmanager
def get_connection():
    """Open a database connection and always close it after the operation."""
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    """Create missing tables without deleting or replacing existing records."""
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                date TEXT NOT NULL,
                description TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                monthly_budget REAL NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings (id, monthly_budget) VALUES (1, 20000)"
        )


def parse_iso_date(value):
    """Return a date only when value is a valid, canonical YYYY-MM-DD string."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
        return parsed if parsed.isoformat() == value else None
    except (TypeError, ValueError):
        return None


def validate_expense_form(form):
    amount_text = form.get("amount", "").strip()
    category = form.get("category", "").strip()
    expense_date = form.get("date", "").strip()
    description = form.get("description", "").strip()

    try:
        amount = float(amount_text)
        if not math.isfinite(amount) or amount <= 0:
            return None, "Amount must be a finite number greater than zero."
    except (TypeError, ValueError):
        return None, "Please enter a valid amount."

    if category not in ALLOWED_CATEGORIES:
        return None, "Please select a valid category."

    if parse_iso_date(expense_date) is None:
        return None, "Please enter a valid date."

    if not description:
        return None, "Description is required."
    if len(description) > 200:
        return None, "Description cannot exceed 200 characters."

    return {
        "amount": amount,
        "category": category,
        "date": expense_date,
        "description": description,
    }, None


def parse_positive_amount(value):
    try:
        amount = float(value)
        if math.isfinite(amount) and amount > 0:
            return amount
    except (TypeError, ValueError):
        pass
    return None


# Initialize at import time as well, so `flask --app main run` works on a fresh setup.
# CREATE TABLE IF NOT EXISTS and INSERT OR IGNORE preserve existing data.
init_db()


@app.route("/")
def home():
    today = date.today()
    month = request.args.get("month", today.strftime("%Y-%m")).strip()
    category = request.args.get("category", "").strip()
    search = request.args.get("q", "").strip()[:100]
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    try:
        parsed_month = datetime.strptime(month, "%Y-%m")
        month = parsed_month.strftime("%Y-%m")
    except ValueError:
        month = today.strftime("%Y-%m")

    filters = ["strftime('%Y-%m', date) = ?"]
    params = [month]

    if category in ALLOWED_CATEGORIES:
        filters.append("category = ?")
        params.append(category)
    else:
        category = ""

    if search:
        filters.append("(description LIKE ? OR category LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    parsed_start = parse_iso_date(start_date) if start_date else None
    parsed_end = parse_iso_date(end_date) if end_date else None
    if start_date and parsed_start is None:
        start_date = ""
    if end_date and parsed_end is None:
        end_date = ""

    if start_date and end_date and parsed_start and parsed_end and parsed_start > parsed_end:
        flash("The start date must be on or before the end date.", "error")
        start_date = ""
        end_date = ""

    if start_date:
        filters.append("date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("date <= ?")
        params.append(end_date)

    where_clause = "WHERE " + " AND ".join(filters)

    with get_connection() as connection:
        expenses = connection.execute(
            f"SELECT * FROM expenses {where_clause} ORDER BY date DESC, id DESC",
            params,
        ).fetchall()

        monthly_total = connection.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE strftime('%Y-%m', date) = ?",
            (month,),
        ).fetchone()[0]

        expense_count = connection.execute(
            "SELECT COUNT(*) FROM expenses WHERE strftime('%Y-%m', date) = ?",
            (month,),
        ).fetchone()[0]

        category_totals = connection.execute(
            """
            SELECT category, SUM(amount) AS total
            FROM expenses
            WHERE strftime('%Y-%m', date) = ?
            GROUP BY category
            ORDER BY total DESC
            """,
            (month,),
        ).fetchall()

        monthly_budget = connection.execute(
            "SELECT monthly_budget FROM settings WHERE id = 1"
        ).fetchone()[0]

        all_time_total = connection.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses"
        ).fetchone()[0]

    budget_remaining = monthly_budget - monthly_total
    budget_percent = min(100.0, monthly_total / monthly_budget * 100) if monthly_budget > 0 else 0.0

    return render_template(
        "index.html",
        expenses=expenses,
        monthly_total=monthly_total,
        monthly_budget=monthly_budget,
        budget_remaining=budget_remaining,
        expense_count=expense_count,
        category_totals=category_totals,
        all_time_total=all_time_total,
        budget_percent=budget_percent,
        categories=sorted(ALLOWED_CATEGORIES),
        selected_month=month,
        selected_category=category,
        search_query=search,
        start_date=start_date,
        end_date=end_date,
        today=today.isoformat(),
    )


@app.route("/add-expense", methods=["POST"])
def add_expense():
    data, error = validate_expense_form(request.form)
    if error:
        flash(error, "error")
        return redirect(url_for("home"))

    with get_connection() as connection:
        connection.execute(
            "INSERT INTO expenses (amount, category, date, description) VALUES (?, ?, ?, ?)",
            (data["amount"], data["category"], data["date"], data["description"]),
        )
    flash("Expense added successfully.", "success")
    return redirect(url_for("home"))


@app.route("/edit-expense/<int:expense_id>", methods=["POST"])
def edit_expense(expense_id):
    data, error = validate_expense_form(request.form)
    if error:
        flash(error, "error")
        return redirect(url_for("home"))

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE expenses
            SET amount = ?, category = ?, date = ?, description = ?
            WHERE id = ?
            """,
            (data["amount"], data["category"], data["date"], data["description"], expense_id),
        )
    if cursor.rowcount == 0:
        abort(404)
    flash("Expense updated successfully.", "success")
    return redirect(url_for("home"))


@app.route("/delete-expense/<int:expense_id>", methods=["POST"])
def delete_expense(expense_id):
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    if cursor.rowcount == 0:
        abort(404)
    flash("Expense deleted successfully.", "success")
    return redirect(url_for("home"))


@app.route("/update-budget", methods=["POST"])
def update_budget():
    monthly_budget = parse_positive_amount(request.form.get("monthly_budget", "").strip())
    if monthly_budget is None:
        flash("Budget must be a valid number greater than zero.", "error")
        return redirect(url_for("home"))

    with get_connection() as connection:
        connection.execute(
            "UPDATE settings SET monthly_budget = ? WHERE id = 1",
            (monthly_budget,),
        )
    flash("Monthly budget updated.", "success")
    return redirect(url_for("home"))


@app.errorhandler(404)
def not_found(_error):
    return render_template(
        "error.html",
        code=404,
        message="The requested item could not be found.",
    ), 404


if __name__ == "__main__":
    # For local development only. Production deployments should use a WSGI server.
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
