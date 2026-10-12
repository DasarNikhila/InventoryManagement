
import os
import io
import csv
import re
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

from bson import ObjectId
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, Response, session, send_file, abort
)
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
)
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4, landscape

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
)

client = MongoClient(
    os.environ["MONGO_URI"],
    serverSelectionTimeoutMS=10000
)
db = client["inventoryDB"]

products = db["products"]
stock_history = db["stock_history"]
sales = db["sales"]
users = db["users"]

LOW_STOCK_LIMIT = 10

DEFAULT_PRODUCTS = [
    ("Blue Ballpoint Pens", "Stationary", 50, 10),
    ("A4 Notebooks", "Stationary", 30, 60),
    ("Stapler", "Stationary", 20, 80),
    ("Spiral Notebook", "Stationary", 30, 50),
    ("Pencils", "Stationary", 40, 5),
    ("Sticky Notes", "Stationary", 25, 35),
    ("Correction Tape", "Stationary", 15, 30),
    ("Ball Pens", "Stationary", 40, 10),
    ("Erasers", "Stationary", 35, 5),
    ("Geometry Box", "Stationary", 10, 120),
    ("Highlighters", "Stationary", 20, 25),
    ("Ruler", "Stationary", 25, 15),
    ("Paper Clips", "Stationary", 50, 20),
    ("Glue Stick", "Stationary", 18, 25),
    ("File Folder", "Stationary", 22, 40),
    ("Wireless Mouse", "Electronics", 15, 450),
    ("USB Drive", "Electronics", 8, 350),
    ("Keyboard", "Electronics", 12, 650),
    ("USB Cable", "Electronics", 18, 150),
    ("Headphones", "Electronics", 10, 799),
    ("Webcam", "Electronics", 6, 1200),
    ("Water Bottle", "Accessories", 25, 180),
    ("Desk Organizer", "Accessories", 7, 250),
]

# Product images are loaded automatically using a remote image service.
# Replace these with your own hosted image URLs for reliable product photos.
IMAGE_KEYWORDS = {
    "pen": "pen", "notebook": "notebook", "stapler": "stapler",
    "pencil": "pencil", "sticky": "stationery", "tape": "tape",
    "eraser": "eraser", "geometry": "stationery",
    "highlighter": "marker", "ruler": "ruler",
    "paper": "paper", "glue": "glue", "folder": "folder",
    "mouse": "computer-mouse", "usb": "usb",
    "keyboard": "keyboard", "headphone": "headphones",
    "webcam": "webcam", "bottle": "water-bottle",
    "organizer": "desk",
}


def now():
    return datetime.now(timezone.utc)


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_float(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def product_image(name):
    name = name.lower()
    keyword = next(
        (value for key, value in IMAGE_KEYWORDS.items() if key in name),
        "product"
    )
    return f"https://placehold.co/120x120/eee9ff/6250d9?text={keyword}"


def get_product(product_id):
    if not ObjectId.is_valid(product_id):
        return None
    return products.find_one({"_id": ObjectId(product_id)})


def normalize(item):
    item["quantity"] = to_int(item.get("quantity"))
    item["price"] = to_float(item.get("price"))
    item["reorder_level"] = to_int(
        item.get("reorder_level"), LOW_STOCK_LIMIT
    )
    item["image_url"] = item.get("image_url") or product_image(
        item.get("name", "product")
    )
    return item


def record(product, action, change, after, reason=""):
    stock_history.insert_one({
        "product_id": product.get("_id"),
        "product_name": product.get("name", "Unknown"),
        "action": action,
        "change": change,
        "quantity_after": after,
        "reason": reason,
        "username": session.get("username", "system"),
        "timestamp": now(),
    })


def initialize_database():
    users.create_index("username", unique=True)

    admin_username = os.getenv("ADMIN_USERNAME")
    admin_password = os.getenv("ADMIN_PASSWORD")

    if admin_username and admin_password:
        if not users.find_one({"username": admin_username}):
            try:
                users.insert_one({
                    "username": admin_username,
                    "password_hash": generate_password_hash(admin_password),
                    "role": "admin",
                    "created_at": now(),
                })
            except DuplicateKeyError:
                pass

    for name, category, quantity, price in DEFAULT_PRODUCTS:
        existing = products.find_one({
            "name": {"$regex": "^" + re.escape(name) + "$", "$options": "i"}
        })
        if existing:
            continue

        document = {
            "name": name,
            "category": category,
            "quantity": quantity,
            "price": float(price),
            "reorder_level": LOW_STOCK_LIMIT,
            "image_url": product_image(name),
            "created_at": now(),
        }
        result = products.insert_one(document)
        document["_id"] = result.inserted_id
        record(document, "Initial stock", quantity, quantity)


# Seed when a request first arrives, including on Render/Gunicorn.
initialized = False


@app.before_request
def setup():
    global initialized

    if not initialized:
        initialize_database()
        initialized = True

    if request.endpoint == "static":
        return

    # CSRF protection for all authenticated POST forms and AJAX requests.
    if request.method == "POST" and request.endpoint != "login":
        expected = session.get("csrf_token")
        supplied = (
            request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token")
        )
        if not expected or not secrets.compare_digest(expected, supplied or ""):
            abort(400, "Invalid form security token")


@app.context_processor
def inject_globals():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(24)
    return {
        "csrf_token": session["csrf_token"],
        "current_user": session.get("username"),
        "current_role": session.get("role"),
    }


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def reply(message, success=True, quantity=None, status=200):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": success,
            "message": message,
            "quantity": quantity,
        }), status

    flash(message, "success" if success else "error")
    return redirect(url_for("index"))


# ---------------- AUTHENTICATION ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = users.find_one({"username": username})

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["username"] = username
            session["role"] = user["role"]
            session["csrf_token"] = secrets.token_hex(24)
            return redirect(url_for("index"))

        flash("Invalid username or password.", "error")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/staff/add", methods=["POST"])
@admin_required
def add_staff():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not username or len(password) < 12:
        flash("Enter a username and password of at least 12 characters.", "error")
        return redirect(url_for("index"))

    try:
        users.insert_one({
            "username": username,
            "password_hash": generate_password_hash(password),
            "role": "staff",
            "created_at": now(),
        })
        flash("Staff account created.", "success")
    except DuplicateKeyError:
        flash("Username already exists.", "error")

    return redirect(url_for("index"))


# ---------------- DASHBOARD ----------------

@app.route("/")
@login_required
def index():
    search = request.args.get("q", "").strip()
    category_filter = request.args.get("category", "")
    status_filter = request.args.get("status", "")

    all_products = [normalize(p) for p in products.find()]
    filtered = []

    for product in all_products:
        if search and search.lower() not in product.get("name", "").lower():
            continue
        if category_filter and product.get("category") != category_filter:
            continue

        qty = product["quantity"]
        limit = product["reorder_level"]

        if status_filter == "low" and not (0 < qty <= limit):
            continue
        if status_filter == "out" and qty != 0:
            continue
        if status_filter == "healthy" and qty <= limit:
            continue

        filtered.append(product)

    filtered.sort(key=lambda p: p.get("name", "").lower())

    total_products = len(all_products)
    total_units = sum(p["quantity"] for p in all_products)
    total_value = sum(p["quantity"] * p["price"] for p in all_products)
    low_stock_count = sum(
        1 for p in all_products if 0 < p["quantity"] <= p["reorder_level"]
    )
    out_of_stock_count = sum(
        1 for p in all_products if p["quantity"] == 0
    )

    category_totals = {}
    for p in all_products:
        category = p.get("category", "Other")
        category_totals[category] = (
            category_totals.get(category, 0) + p["quantity"]
        )

    category_chart = [
        {"category": k, "quantity": v}
        for k, v in sorted(category_totals.items())
    ]
    max_category_quantity = max(
        [c["quantity"] for c in category_chart] or [1]
    )

    recent_history = list(
        stock_history.find().sort("timestamp", -1).limit(5)
    )

    all_sales = list(sales.find())
    total_revenue = sum(to_float(s.get("total")) for s in all_sales)
    units_sold = sum(to_int(s.get("quantity")) for s in all_sales)

    sales_by_product = {}
    for sale in all_sales:
        name = sale.get("product_name", "Unknown")
        sales_by_product[name] = (
            sales_by_product.get(name, 0) + to_int(sale.get("quantity"))
        )

    best_sellers = sorted(
        sales_by_product.items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]

    return render_template(
        "index.html",
        products=filtered,
        categories=sorted({
            p.get("category", "Other") for p in all_products
        }),
        category_chart=category_chart,
        max_category_quantity=max(max_category_quantity, 1),
        total_products=total_products,
        total_units=total_units,
        total_value=total_value,
        low_stock_count=low_stock_count,
        out_of_stock_count=out_of_stock_count,
        recent_history=recent_history,
        total_revenue=total_revenue,
        units_sold=units_sold,
        best_sellers=best_sellers,
        search=search,
        category_filter=category_filter,
        status_filter=status_filter,
    )


# ---------------- PRODUCT MANAGEMENT ----------------

@app.route("/add", methods=["POST"])
@login_required
def add_product():
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    quantity = to_int(request.form.get("quantity"), -1)
    price = to_float(request.form.get("price"), -1)
    reorder_level = to_int(request.form.get("reorder_level"), 10)

    if not name or not category or quantity < 0 or price < 0 or reorder_level < 0:
        return reply("Invalid product details.", False)

    image_url = request.form.get("image_url", "").strip()
    if image_url and not image_url.startswith(("https://", "http://")):
        return reply("Image URL must start with https:// or http://", False)

    item = {
        "name": name,
        "category": category,
        "quantity": quantity,
        "price": price,
        "reorder_level": reorder_level,
        "image_url": image_url or product_image(name),
        "created_at": now(),
    }
    result = products.insert_one(item)
    item["_id"] = result.inserted_id
    record(item, "Product added", quantity, quantity)

    return reply("Product added successfully.")


@app.route("/quantity/<product_id>/<action>", methods=["POST"])
@login_required
def change_quantity(product_id, action):
    item = get_product(product_id)

    if not item or action not in ("increase", "decrease"):
        return reply("Invalid product or action.", False, status=404)

    amount = to_int(request.form.get("amount"), 1)
    if not 1 <= amount <= 10000:
        return reply("Invalid quantity.", False, status=400)

    change = amount if action == "increase" else -amount
    query = {"_id": item["_id"]}

    if change < 0:
        query["quantity"] = {"$gte": amount}

    updated = products.find_one_and_update(
        query,
        {"$inc": {"quantity": change}},
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        return reply("Not enough stock.", False, status=400)

    record(
        updated,
        "Stock increased" if change > 0 else "Stock decreased",
        change,
        updated["quantity"],
    )
    return reply("Stock updated.", True, updated["quantity"])


@app.route("/edit/<product_id>")
@login_required
def edit_product(product_id):
    item = get_product(product_id)
    if not item:
        abort(404)
    return render_template("edit.html", product=normalize(item))


@app.route("/update/<product_id>", methods=["POST"])
@login_required
def update_product(product_id):
    item = get_product(product_id)
    if not item:
        abort(404)

    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    quantity = to_int(request.form.get("quantity"), -1)
    price = to_float(request.form.get("price"), -1)
    reorder_level = to_int(request.form.get("reorder_level"), -1)
    image_url = request.form.get("image_url", "").strip()

    if (
        not name or not category or quantity < 0 or price < 0
        or reorder_level < 0
        or (image_url and not image_url.startswith(("https://", "http://")))
    ):
        flash("Invalid product details.", "error")
        return redirect(url_for("edit_product", product_id=product_id))

    old_quantity = to_int(item.get("quantity"))

    products.update_one({"_id": item["_id"]}, {"$set": {
        "name": name,
        "category": category,
        "quantity": quantity,
        "price": price,
        "reorder_level": reorder_level,
        "image_url": image_url or product_image(name),
    }})

    if quantity != old_quantity:
        record(
            {"_id": item["_id"], "name": name},
            "Quantity edited",
            quantity - old_quantity,
            quantity,
        )

    flash("Product updated.", "success")
    return redirect(url_for("index"))


@app.route("/delete/<product_id>", methods=["POST"])
@admin_required
def delete_product(product_id):
    item = get_product(product_id)
    if not item:
        abort(404)

    result = products.delete_one({"_id": item["_id"]})
    if result.deleted_count:
        record(item, "Product deleted", -to_int(item.get("quantity")), 0)
        flash("Product deleted.", "success")

    return redirect(url_for("index"))


# ---------------- SALES ----------------

@app.route("/sell/<product_id>", methods=["POST"])
@login_required
def sell_product(product_id):
    item = get_product(product_id)
    if not item:
        return reply("Product not found.", False, status=404)

    amount = to_int(request.form.get("quantity"), 1)
    if amount < 1 or amount > 10000:
        return reply("Invalid sales quantity.", False, status=400)

    updated = products.find_one_and_update(
        {"_id": item["_id"], "quantity": {"$gte": amount}},
        {"$inc": {"quantity": -amount}},
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        return reply("Insufficient stock.", False, status=400)

    price = to_float(item.get("price"))
    sales.insert_one({
        "product_id": item["_id"],
        "product_name": item["name"],
        "quantity": amount,
        "unit_price": price,
        "total": round(price * amount, 2),
        "username": session["username"],
        "timestamp": now(),
    })

    record(updated, "Sale", -amount, updated["quantity"], "Customer sale")
    return reply("Sale recorded.", True, updated["quantity"])


# ---------------- STOCK HISTORY ----------------

@app.route("/history")
@login_required
def history():
    history_list = list(
        stock_history.find().sort("timestamp", -1).limit(500)
    )
    return render_template("history.html", history=history_list)


# ---------------- CSV EXPORT ----------------

@app.route("/export")
@login_required
def export_csv():
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Name", "Category", "Quantity", "Unit Price",
        "Stock Value", "Reorder Level"
    ])

    for raw in products.find().sort("name", 1):
        p = normalize(raw)
        writer.writerow([
            p.get("name", ""),
            p.get("category", ""),
            p["quantity"],
            p["price"],
            round(p["quantity"] * p["price"], 2),
            p["reorder_level"],
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                "attachment; filename=stockly_inventory.csv"
        },
    )


# ---------------- PDF REPORT ----------------

@app.route("/report/pdf")
@login_required
def pdf_report():
    items = [normalize(p) for p in products.find().sort("name", 1)]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=25, rightMargin=25,
        topMargin=30, bottomMargin=30,
    )

    styles = getSampleStyleSheet()
    elements = [
        Paragraph("STOCKLY - Inventory Report", styles["Title"]),
        Paragraph(
            "Generated: " + now().strftime("%d %B %Y %H:%M UTC"),
            styles["Normal"],
        ),
        Spacer(1, 18),
    ]

    rows = [[
        "Product", "Category", "Quantity", "Price",
        "Value", "Reorder", "Status"
    ]]

    total_value = 0

    for p in items:
        qty = p["quantity"]
        value = qty * p["price"]
        total_value += value

        status = (
            "Out of stock" if qty == 0
            else "Low stock" if qty <= p["reorder_level"]
            else "Healthy"
        )

        rows.append([
            p.get("name", "")[:30],
            p.get("category", "")[:20],
            str(qty),
            f"Rs. {p['price']:.2f}",
            f"Rs. {value:.2f}",
            str(p["reorder_level"]),
            status,
        ])

    table = Table(
        rows,
        repeatRows=1,
        colWidths=[160, 110, 60, 85, 95, 70, 90],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6254dc")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
            colors.white, colors.HexColor("#f4f4fb")
        ]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 20))
    elements.append(Paragraph(
        f"Total products: {len(items)} | "
        f"Inventory value: Rs. {total_value:,.2f}",
        styles["Heading2"],
    ))

    doc.build(elements)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="stockly_inventory_report.pdf",
        mimetype="application/pdf",
    )


@app.route("/health")
def health():
    try:
        client.admin.command("ping")
        return jsonify({"status": "ok"})
    except Exception:
        return jsonify({"status": "error"}), 503


if __name__ == "__main__":
    app.run(debug=True)
