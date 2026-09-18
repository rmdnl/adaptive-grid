import sqlite3
from pathlib import Path

def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS orders (
      client_order_id TEXT PRIMARY KEY,
      symbol TEXT NOT NULL, side TEXT NOT NULL, grid_index INTEGER NOT NULL,
      price REAL NOT NULL, quantity REAL NOT NULL, status TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS fills (
      trade_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, symbol TEXT NOT NULL,
      side TEXT NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL,
      fee REAL NOT NULL, fee_asset TEXT NOT NULL, event_time TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS equity_snapshots (
      ts TEXT PRIMARY KEY, equity_quote REAL NOT NULL, drawdown_pct REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    con.commit()
    con.close()
