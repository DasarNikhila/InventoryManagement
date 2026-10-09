
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, Response
)
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
from bson.objectid import ObjectId
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import csv
import io
import re

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "replace-this-with-a-secret-key")

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing from your .env file.")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client["inventoryDB"]

products = db["products"]
stock_history = db["stock_history"]

LOW_STOCK_LIMIT = 10


def now_utc():
    return datetime.now(timezone.utc)


def valid_id(value):
    return ObjectId.is_valid(value)


def get_stock_status(quantity):
    if quantity <= 0:
        return "Out of stock"
    if quantity <= LOW_STOCK_LIMIT:
        return "Low stock"
    return "In stock"


def record_movement(product_id, product_name, change, old_quantity,
                    new_quantity, reason):
    """Write an audit record without replacing product data."""
    stock_history.insert_one({
        "product_id": str(product_id),
        "product_name": product_name,
        "change": int(change),
        "old_quantity": int(old_quantity),
        "new_quantity": int(new_quantity),
        "reason": reason,
        "timestamp": now_utc()
    })


def parse_product_form():
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    supplier = request.form.get("supplier", "").strip()

    if not name or not category or not supplier:
        raise ValueError("Please fill in all product fields.")

    try:
        price = Decimal(request.form.get("price", ""))
        quantity = int(request.form.get("quantity", ""))
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid price and whole-number quantity.")

    if not price.is_finite() or price < 0:
        raise ValueError("Price must be a valid non-negative number.")
    if quantity < 0 or quantity > 2_000_000_000:
        raise ValueError("Quantity must be between 0 and 2,000,000,000.")

    return {
        "name": name[:100],
        "category": category[:100],
        "price": float(price),
        "quantity": quantity,
        "supplier": supplier[:100]
    }


@app.route("/")
def index():
    search = request.args.get("q", "").strip()
    category_filter = request.args.get("category", "").strip()
    status_filter = request.args.get("status", "").strip()

    query = {}

    if search:
        safe_search = re.escape(search)
        query["$or"] = [
            {"name": {"$regex": safe_search, "$options": "i"}},
            {"category": {"$regex": safe_search, "$options": "i"}},
            {"supplier": {"$regex": safe_search, "$options": "i"}}
        ]

    if category_filter:
        query["category"] = category_filter

    if status_filter == "out":
        query["quantity"] = 0
    elif status_filter == "low":
        query["quantity"] = {"$gt": 0, "$lte": LOW_STOCK_LIMIT}
    elif status_filter == "in":
        query["quantity"] = {"$gt": LOW_STOCK_LIMIT}

    try:
        # Dashboard figures use the full inventory, not only search results.
        all_inventory = list(products.find({}).limit(10000))
        visible_products = list(products.find(query).sort("name", 1).limit(1000))

        for product in visible_products:
            product["stock_status"] = get_stock_status(
                int(product.get("quantity", 0))
            )

        categories = sorted({
            p.get("category", "")
            for p in all_inventory
            if p.get("category")
        })

        total_products = len(all_inventory)
        total_units = sum(int(p.get("quantity", 0)) for p in all_inventory)
        inventory_value = sum(
            float(p.get("price", 0)) * int(p.get("quantity", 0))
            for p in all_inventory
        )
        low_stock_count = sum(
            1 for p in all_inventory
            if 0 < int(p.get("quantity", 0)) <= LOW_STOCK_LIMIT
        )
        out_of_stock_count = sum(
            1 for p in all_inventory
            if int(p.get("quantity", 0)) <= 0
        )

        category_totals = {}
        for p in all_inventory:
            cat = p.get("category", "Other")
            category_totals[cat] = category_totals.get(cat, 0) + int(
                p.get("quantity", 0)
            )

        category_chart = [
            {"name": name, "quantity": quantity}
            for name, quantity in sorted(
                category_totals.items(),
                key=lambda item: item[1],
                reverse=True
            )[:8]
        ]

        recent_history = list(
            stock_history.find({}).sort("timestamp", -1).limit(5)
        )

        return render_template(
            "index.html",
            products=visible_products,
            search=search,
            category_filter=category_filter,
            status_filter=status_filter,
            categories=categories,
            total_products=total_products,
            total_units=total_units,
            inventory_value=inventory_value,
            low_stock_count=low_stock_count,
            out_of_stock_count=out_of_stock_count,
            category_chart=category_chart,
            recent_history=recent_history,
            low_stock_limit=LOW_STOCK_LIMIT
        )
    except PyMongoError:
        app.logger.exception("Could not load inventory")
        return "Database connection failed. Check Atlas access and application logs.", 503


@app.post("/add")
def add_product():
    try:
        product = parse_product_form()
        result = products.insert_one(product)

        record_movement(
            result.inserted_id, product["name"],
            product["quantity"], 0, product["quantity"],
            "Initial stock / product added"
        )
        flash(f'{product["name"]} added successfully.', "success")
    except ValueError as error:
        flash(str(error), "error")
    except PyMongoError:
        app.logger.exception("Could not add product")
        flash("Could not save the product or its history.", "error")

    return redirect(url_for("index"))


@app.post("/quantity/<product_id>/<action>")
def change_quantity(product_id, action):
    if not valid_id(product_id) or action not in ("increase", "decrease"):
        flash("Invalid product or stock action.", "error")
        return redirect(url_for("index"))

    try:
        selector = {"_id": ObjectId(product_id)}
        if action == "decrease":
            selector["quantity"] = {"$gt": 0}

        change = 1 if action == "increase" else -1

        updated = products.find_one_and_update(
            selector,
            {"$inc": {"quantity": change}},
            return_document=ReturnDocument.AFTER
        )

        if updated is None:
            if products.find_one({"_id": ObjectId(product_id)}):
                flash("Quantity cannot go below zero.", "error")
            else:
                flash("Product not found.", "error")
        else:
            old_quantity = int(updated["quantity"]) - change
            record_movement(
                updated["_id"], updated["name"], change,
                old_quantity, updated["quantity"],
                "Quick increase" if change > 0 else "Quick decrease"
            )
            flash(
                f'{updated["name"]}: quantity is now {updated["quantity"]}.',
                "success"
            )
    except PyMongoError:
        app.logger.exception("Stock adjustment failed")
        flash("Stock adjustment failed. Please try again.", "error")

    return redirect(url_for(
        "index",
        q=request.form.get("q", ""),
        category=request.form.get("category", ""),
        status=request.form.get("status", "")
    ))


@app.route("/edit/<product_id>")
def edit_product(product_id):
    if not valid_id(product_id):
        return "Invalid product ID.", 400

    try:
        product = products.find_one({"_id": ObjectId(product_id)})
    except PyMongoError:
        app.logger.exception("Could not retrieve product")
        return "Database unavailable.", 503

    if product is None:
        return "Product not found.", 404

    return render_template("edit.html", product=product)


@app.post("/update/<product_id>")
def update_product(product_id):
    if not valid_id(product_id):
        return "Invalid product ID.", 400

    try:
        new_data = parse_product_form()
        old = products.find_one({"_id": ObjectId(product_id)})

        if old is None:
            flash("Product not found.", "error")
            return redirect(url_for("index"))

        products.update_one(
            {"_id": ObjectId(product_id)},
            {"$set": new_data}
        )

        old_quantity = int(old.get("quantity", 0))
        new_quantity = int(new_data["quantity"])
        difference = new_quantity - old_quantity

        if difference != 0:
            record_movement(
                old["_id"], new_data["name"], difference,
                old_quantity, new_quantity,
                "Quantity changed through edit form"
            )

        flash("Product updated successfully.", "success")
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("edit_product", product_id=product_id))
    except PyMongoError:
        app.logger.exception("Could not update product")
        flash("Could not update the product.", "error")

    return redirect(url_for("index"))


@app.post("/delete/<product_id>")
def delete_product(product_id):
    if not valid_id(product_id):
        flash("Invalid product ID.", "error")
        return redirect(url_for("index"))

    try:
        product = products.find_one({"_id": ObjectId(product_id)})
        if product is None:
            flash("Product not found.", "error")
        else:
            # Record the deletion before removing the product.
            record_movement(
                product["_id"], product["name"],
                -int(product.get("quantity", 0)),
                int(product.get("quantity", 0)), 0,
                "Product deleted"
            )
            products.delete_one({"_id": product["_id"]})
            flash("Product deleted. Its stock history was retained.", "success")
    except PyMongoError:
        app.logger.exception("Could not delete product")
        flash("Could not delete the product.", "error")

    return redirect(url_for("index"))


@app.route("/history")
def history():
    try:
        movements = list(
            stock_history.find({}).sort("timestamp", -1).limit(500)
        )
        return render_template("history.html", movements=movements)
    except PyMongoError:
        app.logger.exception("Could not load stock history")
        return "Could not load stock history.", 503


@app.route("/export")
def export_csv():
    try:
        inventory = list(products.find({}).sort("name", 1))
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Product", "Category", "Unit Price",
            "Quantity", "Stock Status", "Supplier"
        ])

        for product in inventory:
            quantity = int(product.get("quantity", 0))
            writer.writerow([
                product.get("name", ""),
                product.get("category", ""),
                product.get("price", 0),
                quantity,
                get_stock_status(quantity),
                product.get("supplier", "")
            ])

        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition":
                    "attachment; filename=inventory_report.csv"
            }
        )
    except PyMongoError:
        app.logger.exception("Could not export inventory")
        return "Export failed.", 503


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok", "database": "connected"}
    except PyMongoError:
        return {"status": "error", "database": "unavailable"}, 503


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
