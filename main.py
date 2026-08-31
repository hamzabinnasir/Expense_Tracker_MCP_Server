import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import aiofiles
import aiosqlite
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "expenses.db"
CATEGORIES_PATH = BASE_DIR / "categories.json"

mcp = FastMCP("Expense Tracker")

# In-memory cache of categories.json, loaded once and reused. It's small
# and effectively static, so caching avoids an async disk read on every
# single add_expense call.
_categories_cache: Optional[dict] = None


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------
async def _init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subCategory TEXT,
                note TEXT
            )
            """
        )
        await conn.commit()


async def _load_categories(force_reload: bool = False) -> dict:
    """Read categories.json asynchronously. This is the single source of
    truth for the category taxonomy, and is also exposed as an MCP
    resource below. Cached after first read."""
    global _categories_cache
    if _categories_cache is None or force_reload:
        async with aiofiles.open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            contents = await f.read()
        _categories_cache = json.loads(contents)
    return _categories_cache


# ---------------------------------------------------------------------------
# Category inference (pure/CPU-only -> stays synchronous, just called from
# async tools; string matching over ~19 categories is negligible work)
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


async def infer_category(note: str) -> tuple[str, str]:
    """
    Guess (category, subCategory) from free text (e.g. the expense note),
    by matching words against categories.json.

    Strategy:
      1. Look for an exact/substring match against any subCategory name
         (subCategory names are more specific -> checked first).
      2. Fall back to a match against a top-level category name.
      3. If nothing matches, fall back to Miscellaneous / Uncategorized.
    """
    text = _normalize(note or "")
    if not text:
        return "Miscellaneous", "Uncategorized"

    data = await _load_categories()

    # Pass 1: subCategory match (most specific)
    best_match = None
    best_len = 0
    for entry in data["categories"]:
        for sub in entry["subCategories"]:
            sub_norm = _normalize(sub)
            if sub_norm and sub_norm in text and len(sub_norm) > best_len:
                best_match = (entry["category"], sub)
                best_len = len(sub_norm)
    if best_match:
        return best_match

    # Pass 2: category-level match
    for entry in data["categories"]:
        cat_norm = _normalize(entry["category"])
        if cat_norm and cat_norm in text:
            default_sub = entry["subCategories"][0] if entry["subCategories"] else ""
            return entry["category"], default_sub

    # Pass 3: a few common keyword hints not literally in the taxonomy names
    keyword_map = {
        "uber": ("Transport", "Taxi & Rideshare"),
        "careem": ("Transport", "Taxi & Rideshare"),
        "cab": ("Transport", "Taxi & Rideshare"),
        "taxi": ("Transport", "Taxi & Rideshare"),
        "ride": ("Transport", "Taxi & Rideshare"),
        "airport": ("Transport", "Taxi & Rideshare"),
        "petrol": ("Transport", "Fuel"),
        "gas station": ("Transport", "Fuel"),
        "netflix": ("Entertainment", "Streaming Subscriptions"),
        "spotify": ("Entertainment", "Streaming Subscriptions"),
        "grocery": ("Food", "Groceries"),
        "groceries": ("Food", "Groceries"),
        "coffee": ("Food", "Coffee Shops"),
        "rent": ("Rent", "Monthly Rent"),
        "electricity bill": ("Utilities", "Electricity"),
        "wifi": ("Utilities", "Internet"),
        "internet bill": ("Utilities", "Internet"),
        "doctor": ("Health", "Doctor Visits"),
        "pharmacy": ("Health", "Pharmacy"),
        "medicine": ("Health", "Pharmacy"),
        "gym": ("Health", "Fitness & Gym"),
        "vet": ("Pets", "Vet Visits"),
    }
    for kw, mapped in keyword_map.items():
        if kw in text:
            return mapped

    return "Miscellaneous", "Uncategorized"


async def resolve_category(
    category: Optional[str], subCategory: Optional[str], note: Optional[str]
) -> tuple[str, str]:
    """
    Decide the final (category, subCategory) for an expense:
      - If both are explicitly given, trust the caller.
      - If category is given but subCategory isn't, try to find a matching
        subCategory for that category from the note, else leave it blank.
      - If neither is given, infer both from the note text.
    """
    if category and subCategory:
        return category, subCategory

    if category and not subCategory:
        data = await _load_categories()
        note_norm = _normalize(note or "")
        for entry in data["categories"]:
            if entry["category"].lower() == category.lower():
                for sub in entry["subCategories"]:
                    if _normalize(sub) in note_norm:
                        return category, sub
                return category, ""
        return category, ""

    # Nothing given at all -> full inference from note
    return await infer_category(note or "")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def _parse_date(value: str) -> str:
    """Accepts 'YYYY-MM-DD' and returns it normalized (raises on bad input)."""
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool
async def add_expense(
    amount: float,
    date_str: Optional[str] = None,
    category: Optional[str] = None,
    subCategory: Optional[str] = None,
    note: str = "",
) -> dict:
    """
    Add a new expense.

    Args:
        amount: The amount spent (required).
        date_str: Date in 'YYYY-MM-DD' format. Defaults to today if omitted.
        category: Top-level category (e.g. 'Food'). If omitted, it is
                  guessed from `note` using categories.json.
        subCategory: Sub-category (e.g. 'Coffee Shops'). If omitted, it is
                     guessed from `note` using categories.json.
        note: Free-text description of the expense (e.g. 'Cab ride to airport').

    Returns:
        A dict with the saved expense record, including the resolved
        category/subCategory.
    """
    expense_date = _parse_date(date_str) if date_str else date.today().isoformat()
    final_category, final_subCategory = await resolve_category(category, subCategory, note)

    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            """
            INSERT INTO expenses (date, amount, category, subCategory, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (expense_date, amount, final_category, final_subCategory, note),
        )
        await conn.commit()
        new_id = cursor.lastrowid

    return {
        "status": "success",
        "message": "Expense added successfully",
        "expense": {
            "id": new_id,
            "date": expense_date,
            "amount": amount,
            "category": final_category,
            "subCategory": final_subCategory,
            "note": note,
        },
    }


@mcp.tool
async def load_expenses(
    start_date: Optional[str] = None, end_date: Optional[str] = None
) -> dict:
    """
    Load expenses. If start_date and/or end_date are omitted, loads ALL
    expenses. If both are given, only expenses with date in [start_date,
    end_date] (inclusive) are returned. Dates must be 'YYYY-MM-DD'.

    Args:
        start_date: Optional lower bound date (inclusive).
        end_date: Optional upper bound date (inclusive).

    Returns:
        A dict with status and a list of matching expense records.
    """
    query = "SELECT * FROM expenses"
    params: list = []
    conditions = []

    if start_date:
        conditions.append("date >= ?")
        params.append(_parse_date(start_date))
    if end_date:
        conditions.append("date <= ?")
        params.append(_parse_date(end_date))
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY date DESC, id DESC"

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

    data = [dict(row) for row in rows]
    return {"status": "success", "count": len(data), "data": data}


@mcp.tool
async def summarize_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    group_by: str = "category",
) -> dict:
    """
    Summarize expenses: total spend and a breakdown grouped by 'category'
    (default) or 'subCategory'. Optionally scoped to a date range.

    Args:
        start_date: Optional lower bound date (inclusive), 'YYYY-MM-DD'.
        end_date: Optional upper bound date (inclusive), 'YYYY-MM-DD'.
        group_by: Either 'category' or 'subCategory'.

    Returns:
        A dict with the grand total, the date range applied, and a
        breakdown list sorted by amount descending.
    """
    if group_by not in ("category", "subCategory"):
        return {
            "status": "error",
            "message": "group_by must be 'category' or 'subCategory'",
        }

    query = f"SELECT {group_by} AS grp, SUM(amount) AS total, COUNT(*) AS count FROM expenses"
    params: list = []
    conditions = []

    if start_date:
        conditions.append("date >= ?")
        params.append(_parse_date(start_date))
    if end_date:
        conditions.append("date <= ?")
        params.append(_parse_date(end_date))
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += f" GROUP BY {group_by} ORDER BY total DESC"

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        total_query = "SELECT SUM(amount) AS total FROM expenses"
        if conditions:
            total_query += " WHERE " + " AND ".join(conditions)
        async with conn.execute(total_query, params) as cursor:
            total_row = await cursor.fetchone()
            grand_total = total_row["total"] or 0.0

    breakdown = [
        {"group": row["grp"] or "Uncategorized", "total": row["total"], "count": row["count"]}
        for row in rows
    ]

    return {
        "status": "success",
        "start_date": start_date,
        "end_date": end_date,
        "group_by": group_by,
        "grand_total": grand_total,
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
@mcp.resource("categories://list")
async def categories() -> str:
    """Expose the categories.json taxonomy as a resource, so a client/LLM
    can look up valid categories and subCategories, or use it for its own
    inference before calling add_expense."""
    async with aiofiles.open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
        return await f.read()


@mcp.resource("info://server")
async def info() -> str:
    """Get information about this server."""
    server_info = {
        "name": "Expense Tracker",
        "version": "2.0.0",
        "description": "A local, fully-async MCP server for tracking personal "
        "expenses, with automatic category inference via categories.json.",
        "tools": ["add_expense", "load_expenses", "summarize_expenses"],
        "resources": ["categories://list", "info://server"],
        "storage": str(DB_PATH),
        "author": "Hamza Bin Nasir",
    }
    return json.dumps(server_info, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio

    asyncio.run(_init_db())
    # mcp.run()
    mcp.run(transport="http", host="0.0.0.0", port=8000)  # to run as a remote server