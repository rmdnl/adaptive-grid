from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "3.2.1"

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def connect(path):
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(p); con.row_factory=sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db(path):
    con=connect(path)
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
          client_order_id TEXT PRIMARY KEY, exchange_order_id TEXT, symbol TEXT NOT NULL,
          side TEXT NOT NULL, grid_index INTEGER NOT NULL, price TEXT NOT NULL, quantity TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fills (
          trade_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
          price TEXT NOT NULL, quantity TEXT NOT NULL, fee TEXT NOT NULL, fee_asset TEXT NOT NULL,
          event_time TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS equity_snapshots (
          ts TEXT PRIMARY KEY, equity_quote TEXT NOT NULL, drawdown_pct TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_state (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS risk_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, allowed INTEGER NOT NULL,
          reason TEXT NOT NULL, context_json TEXT NOT NULL
        );
        PRAGMA user_version = 321;
        """)
        con.execute(
            "INSERT OR REPLACE INTO bot_state(key,value) VALUES (?,?)",
            ("schema_version", SCHEMA_VERSION)
        )
        con.commit()
    finally:
        con.close()

def set_state(path,key,value):
    con=connect(path)
    try:
        payload=value if isinstance(value,str) else json.dumps(value,separators=(",",":"))
        con.execute(
            "INSERT INTO bot_state(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key,payload)
        ); con.commit()
    finally: con.close()

def get_state(path,key):
    con=connect(path)
    try:
        row=con.execute("SELECT value FROM bot_state WHERE key=?",(key,)).fetchone()
        return None if row is None else str(row["value"])
    finally: con.close()

def record_risk_event(path,allowed,reason,context):
    con=connect(path)
    try:
        con.execute(
            "INSERT INTO risk_events(ts,allowed,reason,context_json) VALUES (?,?,?,?)",
            (utc_now(),int(allowed),reason,json.dumps(context,default=str))
        ); con.commit()
    finally: con.close()

def record_equity(path,equity_quote,drawdown_pct):
    con=connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO equity_snapshots(ts,equity_quote,drawdown_pct) VALUES (?,?,?)",
            (utc_now(),str(equity_quote),str(drawdown_pct))
        ); con.commit()
    finally: con.close()

def append_csv_trade(path,row):
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    fields=list(row.keys()); new_file=not p.exists()
    with p.open("a",newline="",encoding="utf-8") as fh:
        writer=csv.DictWriter(fh,fieldnames=fields)
        if new_file: writer.writeheader()
        writer.writerow(row)
