from flask import Flask, render_template, request, redirect
from pymongo import MongoClient
from bson.objectid import ObjectId
import os
from pathlib import Path
from dotenv import load_dotenv

# -----------------------------------------
# LOAD .ENV FILE
# -----------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_FILE)

app = Flask(__name__)

# -----------------------------------------
# MONGODB ATLAS CONNECTION
# -----------------------------------------

MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    raise ValueError("MONGO_URI is not set in the .env file")

client = MongoClient(MONGO_URI)

db = client["inventoryDB"]

products = db["products"]


# -----------------------------------------
# HOME PAGE
# -----------------------------------------

@app.route("/")
def index():

    all_products = list(products.find())

    return render_template(
        "index.html",
        products=all_products
    )


# -----------------------------------------
# ADD PRODUCT
# -----------------------------------------

@app.route("/add", methods=["POST"])
def add_product():

    name = request.form["name"]
    category = request.form["category"]
    price = float(request.form["price"])
    quantity = int(request.form["quantity"])
    supplier = request.form["supplier"]

    products.insert_one({
        "name": name,
        "category": category,
        "price": price,
        "quantity": quantity,
        "supplier": supplier
    })

    return redirect("/")


# -----------------------------------------
# DELETE PRODUCT
# -----------------------------------------

@app.route("/delete/<id>")
def delete_product(id):

    products.delete_one({
        "_id": ObjectId(id)
    })

    return redirect("/")


# -----------------------------------------
# EDIT PRODUCT PAGE
# -----------------------------------------

@app.route("/edit/<id>")
def edit_product(id):

    product = products.find_one({
        "_id": ObjectId(id)
    })

    if product is None:
        return "Product not found", 404

    return render_template(
        "edit.html",
        product=product
    )


# -----------------------------------------
# UPDATE PRODUCT
# -----------------------------------------

@app.route("/update/<id>", methods=["POST"])
def update_product(id):

    products.update_one(
        {"_id": ObjectId(id)},
        {
            "$set": {
                "name": request.form["name"],
                "category": request.form["category"],
                "price": float(request.form["price"]),
                "quantity": int(request.form["quantity"]),
                "supplier": request.form["supplier"]
            }
        }
    )

    return redirect("/")


# -----------------------------------------
# RUN FLASK APPLICATION
# -----------------------------------------

if __name__ == "__main__":
    app.run(debug=True)