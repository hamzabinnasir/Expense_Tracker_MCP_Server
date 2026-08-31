import json
import re
from contextlib import asynccontextmanager
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

DB_PATH = Path("/tmp/expenses.db")
CATEGORIES_PATH = BASE_DIR / "categories.json"


# ---------------------------------------------------------------------------
# Database initialization
# ---------------------------------------------------------------------------

CREATE_EXPENSES_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        amount REAL NOT NULL,
        category TEXT NOT NULL,
        subCategory TEXT,
        note TEXT
    )
"""


async def _ensure_table(conn: aiosqlite.Connection) -> None:
    """
    Defensively (re)create the expenses table on an already-open
    connection. This is idempotent (IF NOT EXISTS) and cheap, so it's
    safe to call at the top of every tool call. This guards against
    cases where the FastMCP lifespan (_init_db) never ran for the
    process actually serving a given tool invocation -- e.g. when
    tools are invoked through a proxy that doesn't go through the
    normal mcp.run() startup sequence, or when the DB file was
    reset/recreated without a full server restart.
    """

    await conn.execute(CREATE_EXPENSES_TABLE_SQL)
    await conn.commit()


async def _init_db() -> None:
    """
    Initialize the SQLite database.

    This runs automatically when the FastMCP server starts.
    """

    async with aiosqlite.connect(DB_PATH) as conn:
        await _ensure_table(conn)


# ---------------------------------------------------------------------------
# FastMCP lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def app_lifespan(server):
    """
    Runs once when the FastMCP server starts.

    Works with:
    - FastMCP Inspector
    - Local FastMCP server
    - FastMCP Cloud
    """

    print("Starting Expense Tracker MCP server...")

    await _init_db()

    print(f"Database initialized: {DB_PATH}")

    try:
        yield

    finally:
        print("Shutting down Expense Tracker MCP server...")


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Expense Tracker",
    lifespan=app_lifespan,
)


# ---------------------------------------------------------------------------
# Categories cache
# ---------------------------------------------------------------------------

_categories_cache: Optional[dict] = None


# ---------------------------------------------------------------------------
# Category helpers
# ---------------------------------------------------------------------------

async def _load_categories(force_reload: bool = False) -> dict:
    """
    Load categories.json asynchronously.

    The result is cached after the first read.
    """

    global _categories_cache

    if _categories_cache is None or force_reload:

        async with aiofiles.open(
            CATEGORIES_PATH,
            "r",
            encoding="utf-8",
        ) as f:

            contents = await f.read()

        _categories_cache = json.loads(contents)

    return _categories_cache


def _normalize(text: str) -> str:
    """
    Normalize text for category matching.
    """

    return re.sub(
        r"[^a-z0-9 ]",
        " ",
        text.lower(),
    )


async def infer_category(note: str) -> tuple[str, str]:
    """
    Guess (category, subCategory) from free text.

    Strategy:

    1. Check subCategory names first.
    2. Check top-level category names.
    3. Check common keywords.
    4. Fall back to Miscellaneous / Uncategorized.
    """

    text = _normalize(note or "")

    if not text:
        return "Miscellaneous", "Uncategorized"

    data = await _load_categories()

    # -----------------------------------------------------------------------
    # Pass 1: subCategory match
    # -----------------------------------------------------------------------

    best_match = None
    best_len = 0

    for entry in data["categories"]:

        for sub in entry["subCategories"]:

            sub_norm = _normalize(sub)

            if (
                sub_norm
                and sub_norm in text
                and len(sub_norm) > best_len
            ):
                best_match = (
                    entry["category"],
                    sub,
                )

                best_len = len(sub_norm)

    if best_match:
        return best_match

    # -----------------------------------------------------------------------
    # Pass 2: category-level match
    # -----------------------------------------------------------------------

    for entry in data["categories"]:

        cat_norm = _normalize(entry["category"])

        if cat_norm and cat_norm in text:

            default_sub = (
                entry["subCategories"][0]
                if entry["subCategories"]
                else ""
            )

            return (
                entry["category"],
                default_sub,
            )

    # -----------------------------------------------------------------------
    # Pass 3: common keyword hints
    # -----------------------------------------------------------------------

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
    category: Optional[str],
    subCategory: Optional[str],
    note: Optional[str],
) -> tuple[str, str]:
    """
    Decide the final category and subCategory.
    """

    # Both explicitly provided
    if category and subCategory:
        return category, subCategory

    # Category provided but subCategory missing
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

    # Neither provided -> infer from note
    return await infer_category(note or "")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_date(value: str) -> str:
    """
    Accept common date formats and normalize them to YYYY-MM-DD.
    """

    value = value.strip()

    formats = [
        "%Y-%m-%d",  # 2023-08-31
        "%Y/%m/%d",  # 2023/08/31
        "%m/%d/%Y",  # 08/31/2023
        "%m-%d-%Y",  # 08-31-2023
        "%m/%d/%y",  # 08/31/23
        "%m-%d-%y",  # 08-31-23
        "%d/%m/%Y",  # 31/08/2023
        "%d-%m-%Y",  # 31-08-2023
    ]

    for fmt in formats:

        try:
            return datetime.strptime(
                value,
                fmt,
            ).date().isoformat()

        except ValueError:
            continue

    raise ValueError(
        f"Invalid date format: {value}. "
        "Use a valid date such as 08/31/23, "
        "08/31/2023, or 2023-08-31."
    )


# ---------------------------------------------------------------------------
# Tool: Add Expense
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
        amount:
            The amount spent.

        date_str:
            Date of expense.
            Accepts YYYY-MM-DD, YYYY/MM/DD,
            MM/DD/YYYY, MM/DD/YY, etc.
            Defaults to today's date.

        category:
            Optional top-level category.
            Example: Food, Transport, Health.

        subCategory:
            Optional sub-category.
            If omitted, it can be inferred from the note.

        note:
            Free-text description of the expense.
    """

    expense_date = (
        _parse_date(date_str)
        if date_str
        else date.today().isoformat()
    )

    final_category, final_subCategory = await resolve_category(
        category,
        subCategory,
        note,
    )

    async with aiosqlite.connect(DB_PATH) as conn:

        await _ensure_table(conn)

        cursor = await conn.execute(
            """
            INSERT INTO expenses (
                date,
                amount,
                category,
                subCategory,
                note
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                expense_date,
                amount,
                final_category,
                final_subCategory,
                note,
            ),
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


# ---------------------------------------------------------------------------
# Tool: Load Expenses
# ---------------------------------------------------------------------------

@mcp.tool
async def load_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """
    Load expenses.

    Optional filters:
    - start_date
    - end_date
    - category

    Results are ordered by date descending
    and then id descending.
    """

    query = "SELECT * FROM expenses"

    params: list = []
    conditions = []

    # -----------------------------------------------------------------------
    # Date filters
    # -----------------------------------------------------------------------

    if start_date:

        conditions.append("date >= ?")

        params.append(
            _parse_date(start_date)
        )

    if end_date:

        conditions.append("date <= ?")

        params.append(
            _parse_date(end_date)
        )

    # -----------------------------------------------------------------------
    # Category filter
    # -----------------------------------------------------------------------

    if category:

        conditions.append(
            "LOWER(category) = LOWER(?)"
        )

        params.append(category.strip())

    # -----------------------------------------------------------------------
    # WHERE
    # -----------------------------------------------------------------------

    if conditions:

        query += (
            " WHERE "
            + " AND ".join(conditions)
        )

    # -----------------------------------------------------------------------
    # Ordering
    # -----------------------------------------------------------------------

    query += " ORDER BY date DESC, id DESC"

    # -----------------------------------------------------------------------
    # Execute
    # -----------------------------------------------------------------------

    async with aiosqlite.connect(DB_PATH) as conn:

        await _ensure_table(conn)

        conn.row_factory = aiosqlite.Row

        async with conn.execute(
            query,
            params,
        ) as cursor:

            rows = await cursor.fetchall()

    data = [
        dict(row)
        for row in rows
    ]

    # -----------------------------------------------------------------------
    # Calculate total
    # -----------------------------------------------------------------------

    total_amount = sum(
        float(row["amount"])
        for row in rows
    )

    return {
        "status": "success",
        "count": len(data),
        "total_amount": total_amount,
        "start_date": start_date,
        "end_date": end_date,
        "category": category,
        "data": data,
    }


# ---------------------------------------------------------------------------
# Tool: Summarize Expenses
# ---------------------------------------------------------------------------

@mcp.tool
async def summarize_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    group_by: str = "category",
) -> dict:
    """
    Summarize expenses.

    Optional filters:
    - start_date
    - end_date
    - category

    group_by:
    - category
    - subCategory

    The category parameter filters the expenses BEFORE
    grouping and calculating the total.

    Examples:

    1. Total Food expenses:
       category="Food"

    2. Food expenses during August:
       start_date="08/01/2023"
       end_date="08/31/2023"
       category="Food"

    3. Food expenses grouped by subCategory:
       category="Food"
       group_by="subCategory"
    """

    # -----------------------------------------------------------------------
    # Validate group_by
    # -----------------------------------------------------------------------

    if group_by not in (
        "category",
        "subCategory",
    ):

        return {
            "status": "error",
            "message": (
                "group_by must be "
                "'category' or 'subCategory'"
            ),
        }

    # -----------------------------------------------------------------------
    # Build query
    # -----------------------------------------------------------------------

    query = (
        f"SELECT {group_by} AS grp, "
        "SUM(amount) AS total, "
        "COUNT(*) AS count "
        "FROM expenses"
    )

    params: list = []
    conditions = []

    # -----------------------------------------------------------------------
    # Date filters
    # -----------------------------------------------------------------------

    if start_date:

        conditions.append("date >= ?")

        params.append(
            _parse_date(start_date)
        )

    if end_date:

        conditions.append("date <= ?")

        params.append(
            _parse_date(end_date)
        )

    # -----------------------------------------------------------------------
    # Category filter
    # -----------------------------------------------------------------------

    if category:

        conditions.append(
            "LOWER(category) = LOWER(?)"
        )

        params.append(
            category.strip()
        )

    # -----------------------------------------------------------------------
    # WHERE
    # -----------------------------------------------------------------------

    if conditions:

        query += (
            " WHERE "
            + " AND ".join(conditions)
        )

    # -----------------------------------------------------------------------
    # GROUP BY + ORDER BY
    # -----------------------------------------------------------------------

    query += (
        f" GROUP BY {group_by} "
        "ORDER BY total DESC"
    )

    # -----------------------------------------------------------------------
    # Execute queries
    # -----------------------------------------------------------------------

    async with aiosqlite.connect(DB_PATH) as conn:

        await _ensure_table(conn)

        conn.row_factory = aiosqlite.Row

        # ---------------------------------------------------------------
        # Grouped results
        # ---------------------------------------------------------------

        async with conn.execute(
            query,
            params,
        ) as cursor:

            rows = await cursor.fetchall()

        # ---------------------------------------------------------------
        # Total amount
        # ---------------------------------------------------------------

        total_query = (
            "SELECT COALESCE(SUM(amount), 0) AS total "
            "FROM expenses"
        )

        if conditions:

            total_query += (
                " WHERE "
                + " AND ".join(conditions)
            )

        async with conn.execute(
            total_query,
            params,
        ) as cursor:

            total_row = await cursor.fetchone()

            grand_total = float(
                total_row["total"]
                or 0.0
            )

    # -----------------------------------------------------------------------
    # Format breakdown
    # -----------------------------------------------------------------------

    breakdown = [
        {
            "group": row["grp"] or "Uncategorized",
            "total": float(row["total"] or 0.0),
            "count": row["count"],
        }
        for row in rows
    ]

    # -----------------------------------------------------------------------
    # Return result
    # -----------------------------------------------------------------------

    return {
        "status": "success",
        "start_date": start_date,
        "end_date": end_date,
        "category": category,
        "group_by": group_by,
        "grand_total": grand_total,
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# Resource: Categories
# ---------------------------------------------------------------------------

@mcp.resource("categories://list")
async def categories() -> str:
    """
    Expose categories.json as an MCP resource.
    """

    async with aiofiles.open(
        CATEGORIES_PATH,
        "r",
        encoding="utf-8",
    ) as f:

        return await f.read()


# ---------------------------------------------------------------------------
# Resource: Server Info
# ---------------------------------------------------------------------------

@mcp.resource("info://server")
async def info() -> str:
    """
    Get information about the Expense Tracker MCP server.
    """

    server_info = {
        "name": "Expense Tracker",
        "version": "2.1.1",
        "description": (
            "A fully async MCP server for tracking "
            "personal expenses with automatic category "
            "inference and expense filtering."
        ),
        "tools": [
            "add_expense",
            "load_expenses",
            "summarize_expenses",
        ],
        "resources": [
            "categories://list",
            "info://server",
        ],
        "storage": str(DB_PATH),
        "author": "Hamza Bin Nasir",
    }

    return json.dumps(
        server_info,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Local entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()