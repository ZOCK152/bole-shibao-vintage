from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("DATABASE_PATH", str(BASE_DIR / "products.db")))
LEGACY_JSON_FILE = BASE_DIR / "inventory.json"
IMAGE_UPLOAD_DIR = BASE_DIR / "static" / "images"
PLACEHOLDER_IMAGE = "images/placeholder.svg"
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123456")

CONTACT_INFO = {
    "wechat": "GMMG528",
    "whatsapp": "待补充",
    "telegram": "待补充",
    "phone": "待补充",
    "email": "待补充",
}


def ensure_upload_dir() -> None:
    IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    ensure_upload_dir()
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                model TEXT NOT NULL,
                waist TEXT NOT NULL,
                length TEXT NOT NULL,
                color TEXT DEFAULT '',
                product_condition TEXT DEFAULT '',
                price TEXT DEFAULT '',
                stock_status TEXT DEFAULT '有货',
                origin TEXT DEFAULT '',
                description TEXT DEFAULT '',
                main_image TEXT DEFAULT '',
                back_image TEXT DEFAULT '',
                gallery_images TEXT DEFAULT '[]',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.commit()
        migrate_legacy_inventory(db)
    finally:
        db.close()


def migrate_legacy_inventory(db: sqlite3.Connection) -> None:
    row = db.execute("SELECT COUNT(*) AS total FROM products").fetchone()
    if row and row["total"] > 0:
        return
    if not LEGACY_JSON_FILE.exists():
        return

    try:
        legacy_items = json.loads(LEGACY_JSON_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return

    if not isinstance(legacy_items, list):
        return

    for item in legacy_items:
        if not isinstance(item, dict):
            continue
        images = item.get("images") or {}
        gallery_images = images.get("gallery") or []
        main_image = images.get("main", PLACEHOLDER_IMAGE)
        back_image = images.get("back", "")
        db.execute(
            """
            INSERT OR IGNORE INTO products (
                code, model, waist, length, color, product_condition, price,
                stock_status, origin, description, main_image, back_image, gallery_images
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.get("code", "")).strip(),
                str(item.get("model", "")).strip(),
                str(item.get("waist", "")).strip(),
                str(item.get("length", "")).strip(),
                str(item.get("color", "")).strip(),
                str(item.get("condition", "")).strip(),
                str(item.get("price", "")).strip(),
                str(item.get("stock_status", "有货")).strip() or "有货",
                str(item.get("origin", "")).strip(),
                str(item.get("description", "")).strip(),
                str(main_image).strip() or PLACEHOLDER_IMAGE,
                str(back_image).strip(),
                json.dumps(gallery_images, ensure_ascii=False),
            ),
        )
    db.commit()


def save_uploaded_image(file_storage: FileStorage | None) -> str:
    if file_storage is None or not file_storage.filename:
        return ""

    ensure_upload_dir()
    filename = secure_filename(file_storage.filename)
    if not filename:
        return ""

    unique_name = f"{uuid.uuid4().hex}_{filename}"
    destination = IMAGE_UPLOAD_DIR / unique_name
    file_storage.save(destination)
    return f"images/{unique_name}"


def parse_gallery_images(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value if str(item).strip()]


def build_display_images(
    main_image: str,
    back_image: str,
    gallery_images: list[str],
) -> list[str]:
    images: list[str] = []
    for image in [main_image, back_image, *gallery_images]:
        cleaned = str(image).strip()
        if cleaned and cleaned not in images:
            images.append(cleaned)
    return images or [PLACEHOLDER_IMAGE]


def serialize_product(row: sqlite3.Row) -> dict[str, Any]:
    gallery_images = parse_gallery_images(row["gallery_images"])
    main_image = row["main_image"] or PLACEHOLDER_IMAGE
    back_image = row["back_image"] or ""
    return {
        "id": row["id"],
        "code": row["code"],
        "model": row["model"],
        "waist": row["waist"],
        "length": row["length"],
        "color": row["color"] or "未填写",
        "condition": row["product_condition"] or "默认微瑕",
        "price": row["price"] or "私聊询价",
        "stock_status": row["stock_status"] or "有货",
        "origin": row["origin"] or "",
        "description": row["description"] or "",
        "main_image": main_image,
        "back_image": back_image,
        "gallery_images": gallery_images,
        "detail_images": build_display_images(main_image, back_image, gallery_images),
    }


def normalize_search_text(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def digits_only(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


def filter_products(products: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    normalized_query = normalize_search_text(query)
    numeric_query = digits_only(query)
    if not normalized_query:
        return products

    filtered_products: list[dict[str, Any]] = []
    for product in products:
        code = normalize_search_text(product["code"])
        code_digits = digits_only(product["code"])
        model_digits = digits_only(product["model"])
        waist_digits = digits_only(product["waist"])
        length_digits = digits_only(product["length"])
        combined_digits = f"{model_digits}{waist_digits}{length_digits}"
        model_waist_digits = f"{model_digits}{waist_digits}"
        searchable_text = normalize_search_text(
            f"{product['code']} {product['model']} {product['waist']} {product['length']} "
            f"{product['color']} {product['description']}"
        )

        matches_text = (
            normalized_query in code
            or normalized_query in searchable_text
            or normalized_query in combined_digits
            or normalized_query in model_waist_digits
        )
        matches_digits = bool(
            numeric_query
            and (
                numeric_query in code_digits
                or combined_digits.startswith(numeric_query)
                or model_waist_digits.startswith(numeric_query)
            )
        )

        if matches_text or matches_digits:
            filtered_products.append(product)

    return filtered_products


def fetch_all_products(include_sold: bool = True) -> list[dict[str, Any]]:
    db = get_db()
    if include_sold:
        rows = db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM products WHERE stock_status != ? ORDER BY id DESC",
            ("已售",),
        ).fetchall()
    return [serialize_product(row) for row in rows]


def fetch_product_by_code(code: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM products WHERE code = ?", (code,)).fetchone()
    return serialize_product(row) if row else None


def fetch_product_by_id(product_id: int) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    return serialize_product(row) if row else None


def admin_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)

    return wrapped_view


def collect_product_form_data(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}

    main_image = save_uploaded_image(request.files.get("main_image")) or existing.get(
        "main_image", ""
    )
    back_image = save_uploaded_image(request.files.get("back_image")) or existing.get(
        "back_image", ""
    )

    uploaded_gallery = [
        saved_path
        for file_storage in request.files.getlist("gallery_images")
        if (saved_path := save_uploaded_image(file_storage))
    ]
    gallery_images = uploaded_gallery or existing.get("gallery_images", [])

    return {
        "code": request.form.get("code", "").strip(),
        "model": request.form.get("model", "").strip(),
        "waist": request.form.get("waist", "").strip(),
        "length": request.form.get("length", "").strip(),
        "color": request.form.get("color", "").strip(),
        "condition": request.form.get("condition", "").strip(),
        "price": request.form.get("price", "").strip(),
        "stock_status": request.form.get("stock_status", "有货").strip() or "有货",
        "origin": request.form.get("origin", "").strip(),
        "description": request.form.get("description", "").strip(),
        "main_image": main_image or PLACEHOLDER_IMAGE,
        "back_image": back_image,
        "gallery_images": gallery_images,
    }


def validate_product_form(data: dict[str, Any], current_id: int | None = None) -> str | None:
    required_fields = ["code", "model", "waist", "length"]
    if any(not data[field] for field in required_fields):
        return "编码、型号、腰围、裤长为必填项。"

    query = "SELECT id FROM products WHERE code = ?"
    params: tuple[Any, ...] = (data["code"],)
    if current_id is not None:
        query += " AND id != ?"
        params = (data["code"], current_id)

    existing = get_db().execute(query, params).fetchone()
    if existing:
        return "该编码已存在，请更换后再保存。"

    if data["stock_status"] not in {"有货", "已售"}:
        return "库存状态只能是“有货”或“已售”。"

    return None


@app.route("/")
def index():
    search_query = request.args.get("q", "").strip()
    products = filter_products(fetch_all_products(include_sold=True), search_query)
    return render_template(
        "index.html",
        products=products,
        contact_info=CONTACT_INFO,
        search_query=search_query,
    )


@app.route("/product/<code>")
def product_detail(code: str):
    product = fetch_product_by_code(code)
    if product is None:
        abort(404)
    return render_template(
        "product_detail.html",
        product=product,
        contact_info=CONTACT_INFO,
    )


@app.route("/contact")
def contact():
    return render_template("contact.html", contact_info=CONTACT_INFO)


@app.route("/service")
def service():
    return render_template("service.html", contact_info=CONTACT_INFO)


@app.route("/admin")
@admin_required
def admin_home():
    return redirect(url_for("admin_products"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            flash("登录成功。")
            return redirect(url_for("admin_products"))
        flash("用户名或密码错误。")

    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    flash("你已退出后台。")
    return redirect(url_for("admin_login"))


@app.route("/admin/products")
@admin_required
def admin_products():
    return render_template("admin_products.html", products=fetch_all_products(include_sold=True))


@app.route("/admin/products/new", methods=["GET", "POST"])
@admin_required
def admin_product_new():
    if request.method == "POST":
        form_data = collect_product_form_data()
        error = validate_product_form(form_data)
        if error:
            flash(error)
            return render_template("admin_product_form.html", product=form_data, is_edit=False)

        get_db().execute(
            """
            INSERT INTO products (
                code, model, waist, length, color, product_condition, price,
                stock_status, origin, description, main_image, back_image, gallery_images
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                form_data["code"],
                form_data["model"],
                form_data["waist"],
                form_data["length"],
                form_data["color"],
                form_data["condition"],
                form_data["price"],
                form_data["stock_status"],
                form_data["origin"],
                form_data["description"],
                form_data["main_image"],
                form_data["back_image"],
                json.dumps(form_data["gallery_images"], ensure_ascii=False),
            ),
        )
        get_db().commit()
        flash("商品已新增。")
        return redirect(url_for("admin_products"))

    empty_product = {
        "code": "",
        "model": "",
        "waist": "",
        "length": "",
        "color": "",
        "condition": "",
        "price": "",
        "stock_status": "有货",
        "origin": "",
        "description": "",
        "main_image": PLACEHOLDER_IMAGE,
        "back_image": "",
        "gallery_images": [],
    }
    return render_template("admin_product_form.html", product=empty_product, is_edit=False)


@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_product_edit(product_id: int):
    product = fetch_product_by_id(product_id)
    if product is None:
        abort(404)

    if request.method == "POST":
        form_data = collect_product_form_data(existing=product)
        error = validate_product_form(form_data, current_id=product_id)
        if error:
            flash(error)
            form_data["id"] = product_id
            form_data["detail_images"] = build_display_images(
                form_data["main_image"],
                form_data["back_image"],
                form_data["gallery_images"],
            )
            return render_template("admin_product_form.html", product=form_data, is_edit=True)

        get_db().execute(
            """
            UPDATE products
            SET code = ?, model = ?, waist = ?, length = ?, color = ?,
                product_condition = ?, price = ?, stock_status = ?, origin = ?,
                description = ?, main_image = ?, back_image = ?, gallery_images = ?
            WHERE id = ?
            """,
            (
                form_data["code"],
                form_data["model"],
                form_data["waist"],
                form_data["length"],
                form_data["color"],
                form_data["condition"],
                form_data["price"],
                form_data["stock_status"],
                form_data["origin"],
                form_data["description"],
                form_data["main_image"],
                form_data["back_image"],
                json.dumps(form_data["gallery_images"], ensure_ascii=False),
                product_id,
            ),
        )
        get_db().commit()
        flash("商品已更新。")
        return redirect(url_for("admin_products"))

    return render_template("admin_product_form.html", product=product, is_edit=True)


@app.route("/admin/products/<int:product_id>/delete", methods=["POST"])
@admin_required
def admin_product_delete(product_id: int):
    get_db().execute("DELETE FROM products WHERE id = ?", (product_id,))
    get_db().commit()
    flash("商品已删除。")
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<int:product_id>/stock", methods=["POST"])
@admin_required
def admin_product_stock(product_id: int):
    stock_status = request.form.get("stock_status", "").strip()
    if stock_status not in {"有货", "已售"}:
        flash("库存状态无效。")
        return redirect(url_for("admin_products"))

    get_db().execute(
        "UPDATE products SET stock_status = ? WHERE id = ?",
        (stock_status, product_id),
    )
    get_db().commit()
    flash("库存状态已更新。")
    return redirect(url_for("admin_products"))


def bootstrap() -> None:
    init_db()


bootstrap()


if __name__ == "__main__":
    if "--init-db" in sys.argv:
        init_db()
        print(f"Database initialized at {DATABASE}")
        raise SystemExit(0)

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
