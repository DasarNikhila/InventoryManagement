from flask import Flask, render_template, request, redirect
from pymongo import MongoClient
from bson.objectid import ObjectId

app = Flask(__name__)

# --------------------------------
# CONNECT TO MONGODB
# --------------------------------

client = MongoClient("mongodb://localhost:27017/")

# Database
db = client["inventoryDB"]

# Collection
products = db["products"]


# --------------------------------
# HOME PAGE
# --------------------------------

@app.route("/")
def index():

    # Convert MongoDB Cursor into a Python list
    all_products = list(products.find())

    return render_template(
        "index.html",
        products=all_products
    )


# --------------------------------
# ADD PRODUCT
# --------------------------------

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


# --------------------------------
# DELETE PRODUCT
# --------------------------------

@app.route("/delete/<id>")
def delete_product(id):

    products.delete_one({
        "_id": ObjectId(id)
    })

    return redirect("/")


# --------------------------------
# EDIT PRODUCT PAGE
# --------------------------------

@app.route("/edit/<id>")
def edit_product(id):

    product = products.find_one({
        "_id": ObjectId(id)
    })

    return render_template(
        "edit.html",
        product=product
    )


# --------------------------------
# UPDATE PRODUCT
# --------------------------------

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


# --------------------------------
# RUN APPLICATION
# --------------------------------

if __name__ == "__main__":
    app.run(debug=True)