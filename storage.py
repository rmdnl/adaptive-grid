import sqlite3
from pathlib import Path

def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS orders (
      client_order_id TEXT PRIMARY KEY,
      symbol TEXT,
      side TEXT,
      grid_index INTEGER,
      price REAL,
      quantity REAL,
      status TEXT,
      created_at TEXT,
      updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS fills (
      trade_id TEXT PRIMARY KEY,
      order_id TEXT,
      symbol TEXT,
      side TEXT,
      price REAL,
      quantity REAL,
      fee REAL,
      fee_asset TEXT,
      event_time TEXT
    );
    CREATE TABLE IF NOT EXISTS equity_snapshots (
      ts TEXT PRIMARY KEY,
      equity_quote REAL,
      drawdown_pct REAL
    );
    CREATE TABLE IF NOT EXISTS bot_state (
      key TEXT PRIMARY KEY,
      value TEXT
    );
    ''')
    con.commit()
    con.close()
