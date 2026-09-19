# 🤖 Adaptive Grid Bot v3.2.1
### Binance Spot • Dry Run • Anti-Rekt Edition™ 🗿📉

Selamat datang di bot grid yang **belum sok kaya**.

Bot ini dibuat buat Binance **SPOT**, bukan futures, bukan leverage, bukan margin, bukan jurus "all in karena feeling gue kuat". 😭

Misi utamanya:

> **Jangan RUG dulu. Profit belakangan.**

Kalau kondisi market jelek, grid jelek, fee terlalu gede, atau risk engine bilang **NOPE**, bot akan diem.

Dan iya, itu fitur. Bukan bug. 🗿

---

## 🧠 Apa ini sebenarnya?

Grid trading membagi range harga menjadi beberapa level.

```text
110.00 ── SELL
109.34 ── SELL
108.69 ── SELL
108.04 ── SELL
107.40 ── SELL
...
100.00 ── BUY
```

Ketika harga mondar-mandir di dalam range, bot nantinya akan mencoba mengambil selisih antar-grid.

**Tapi:** v3.2.1 masih **DRY RUN**.

Jadi bot belum ngeklik tombol beli/jual beneran.

Dompet lu aman dari bot ini. Untuk sekarang. 😭

---

# 🚨 STATUS v3.2.1

```text
SPOT ONLY             ✅
LEVERAGE              ❌
FUTURES               ❌
MARGIN                ❌
MARTINGALE            ❌
AVERAGING AGRESIF     ❌

DRY RUN               ✅
LIVE ORDER            ❌
RECONCILIATION        ❌
PARTIAL FILL ENGINE   ❌
CRASH RECOVERY        ❌
USER DATA WEBSOCKET   ❌
```

Artinya:

**JANGAN nyalain live trading.**

Bahkan kalau lu merasa:

> "Tenang bro, gue tau risikonya."

Bot:

> "Gue juga tau. Tetap nggak." 🗿

---

# 🎯 Aturan grid yang dikunci

Default:

```yaml
step_pct: 0.006
```

Artinya jarak gross antar-grid:

```text
0.60%
```

Target net:

```text
MINIMUM     0.30%
PREFERRED   0.30% - 0.40%
```

Bot menghitung:

```text
gross movement
      ↓
fee BUY
      ↓
fee SELL
      ↓
slippage
      ↓
NET PROFIT
```

Kalau:

```text
NET < 0.30%
```

maka:

```text
GRID STATUS: 💀 BLOCKED
```

Tidak ada negosiasi.

Tidak ada:

> "Coba aja dulu siapa tau profit."

Nope.

---

# 💸 Contoh matematika

Dengan:

```text
Grid step           = 0.60%
Maker fee BUY       = 0.10%
Maker fee SELL      = 0.10%
Round-trip slippage = 0.05%
```

Net teoritis sekitar:

```text
≈ 0.348%
```

Jadi:

```text
0.60% gross
   ↓
fee
   ↓
slippage
   ↓
≈ 0.348% net
```

Ini **bukan janji profit**.

Market tidak membaca README. 😭

---

# 🛡️ Risk Engine

Risk engine adalah satpam klub.

Kalau satu saja kondisi penting bilang:

```text
NO
```

maka:

```text
ORDER PLAN = BLOCKED
```

Pengaman utama:

### 1. Harga di luar range

Kalau:

```text
LOWER_PRICE = 100
UPPER_PRICE = 110
```

order pada:

```text
99.99
110.01
```

ditolak.

Tidak ada:

> "Dikit doang bro."

Dikit di market bisa jadi awal episode 17. 🗿

### 2. Range-break buffer

Default:

```yaml
range_break_buffer_pct: 0.01
```

atau:

```text
±1%
```

**Buffer bukan izin order di luar range.**

Buffer hanya untuk deteksi range break / kill condition.

### 3. Equity drawdown

Default:

```yaml
max_equity_drawdown_pct: 0.02
```

Artinya:

```text
DD >= 2%
   ↓
KILL
```

Risk engine tidak akan bilang:

> "Santai, nanti juga balik."

Dia bukan teman tongkrongan. 😭

### 4. Market filter

Bot mengecek:

```text
ADX
ATR %
Bollinger Width
Volume Spike
```

Kalau kondisi terlalu agresif:

```text
GRID: "nah gue cabut dulu"
```

---

# 📏 Auto Range

Mode default:

```yaml
range:
  mode: auto
```

Bot membaca candle Binance yang sudah **closed**.

Bukan candle yang masih joget.

Range kandidat menggunakan:

```text
Low quantile
High quantile
ADX
ATR
Bollinger Width
Volume
Position harga
```

Lalu dibuat:

```text
Range Quality Score
```

Kalau quality terlalu rendah:

```text
RANGE = BLOCKED
```

Jadi bot tidak dipaksa trading cuma karena:

> "Udah jalan nih bot, masa nggak entry."

Itu mental FOMO.

Kita tidak membiarkan FOMO punya akses ke API key. 🔐

---

# 🧮 Grid Engine

Grid menggunakan **geometric grid**.

Default:

```text
step = 0.60%
```

Setiap level dihitung:

```text
next_price = current_price × 1.006
```

Semua cell dicek.

Kalau satu cell saja:

```text
< 0.30%
```

maka:

```text
GRID = BLOCKED
```

Kita tidak mau:

> "9 grid cuan, 1 grid jadi anak bawang."

---

# 💰 Fee Engine

Bot mencoba mengambil fee aktual dari Binance.

Yang diperhatikan:

```text
standardCommission
specialCommission
taxCommission
```

Discount tidak langsung dianggap tersedia.

Kalau data fee tidak lengkap, bot memakai fallback config:

```yaml
maker_fee_fallback: 0.001
taker_fee_fallback: 0.001
```

Kita tidak mau:

```text
"fee gue pasti murah kok"
```

lalu realita:

```text
SURPRISE 🎁
```

---

# 🧱 Binance Symbol Rules

Bot membaca aturan symbol dari `exchangeInfo`.

Contohnya:

```text
PRICE_FILTER
LOT_SIZE
MARKET_LOT_SIZE
MIN_NOTIONAL
NOTIONAL
PERCENT_PRICE
PERCENT_PRICE_BY_SIDE
MAX_NUM_ORDERS
MAX_NUM_ALGO_ORDERS
```

Karena exchange itu bukan:

> "Masuk aja bang."

Exchange itu:

> "Formulirnya kurang satu." 🗿

---

# 🧪 DRY RUN

Default:

```yaml
environment:
  mode: testnet
  dry_run: true
  allow_live_execution: false
```

Jalankan:

```bash
python main.py
```

Alurnya:

```text
market data
    ↓
indikator
    ↓
range
    ↓
grid
    ↓
net profit
    ↓
risk
    ↓
state/log
    ↓
TIDAK ORDER
```

Bot boleh banyak ngomong.

Tapi belum punya izin pencet tombol. 😭

---

# 📦 Install

Linux / Armbian:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy:

```bash
cp .env.example .env
```

API key wajib:

```text
READ       ✅
WITHDRAW  ❌
```

Saat order engine nanti dibuat, permission Spot Trading saja yang diperlukan.

**Withdrawal jangan pernah dikasih.**

Bot trading tidak perlu jadi bendahara. 🗿

---

# 🧪 Test

Sebelum main dengan market:

```bash
pytest -q
```

Target:

```text
29 passed
```

Kalau test gagal:

```text
JANGAN LANJUT
```

Bukan:

> "Ah cuma satu test."

Satu baut hilang di motor juga bisa bikin perjalanan jadi lore. 😭

---

# 🧪 Binance Testnet

Gunakan Binance Spot Testnet untuk tahap berikutnya.

Default endpoint:

```text
https://testnet.binance.vision/api
```

Testnet bisa mengalami reset.

Jadi:

```text
balance testnet ≠ uang asli
hasil testnet ≠ jaminan hasil live
```

Testnet adalah tempat latihan.

Bukan mesin ramalan masa depan. 🔮

---

# 📊 Contoh output

```text
Result:
  Risk decision : PASS
  Reason        : PASS
  Price         : 650.1234
  Range         : 620 -> 660
  Grid cells    : 10
  Net/grid      : 0.3487%
  Range quality : 78.50/100
  Fee source    : ACCOUNT_COMMISSION_CONSERVATIVE
  Execution     : DRY RUN, no order placement
```

Kalau kondisi jelek:

```text
Result:
  Risk decision : BLOCK
  Reason        : NET_PROFIT_BELOW_HARD_MIN
                  | MARKET_FILTER_BLOCK:ADX
```

Bot tidak memaksakan grid.

Karena:

```text
NO TRADE
```

juga merupakan keputusan trading.

Kadang posisi terbaik adalah:

```text
☕ duduk
📈 lihat chart
🗿 jangan pencet apa-apa
```

---

# 🗂️ Struktur project

```text
adaptive-grid/
├── main.py
├── config.yaml
├── config_loader.py
├── market_data.py
├── indicators.py
├── range_engine.py
├── grid_engine.py
├── profit_model.py
├── fee_model.py
├── risk_engine.py
├── symbol_rules.py
├── storage.py
├── requirements.txt
├── .env.example
├── .gitignore
├── sample_output.txt
└── tests/
    ├── test_config.py
    ├── test_grid.py
    ├── test_indicators.py
    ├── test_profit.py
    ├── test_range.py
    ├── test_risk.py
    ├── test_storage.py
    ├── test_symbol_rules.py
    └── test_fee.py
```

Yang tidak perlu masuk Git:

```text
.env
.venv/
__pycache__/
.pytest_cache/
*.sqlite3
logs/
```

Kalau API key ikut ke-upload:

```text
GitHub: 👁️
API key: 👁️👁️
Lu: 💀
```

---

# 🚧 Roadmap

Urutannya sengaja tidak langsung:

```text
v3.2.1
  ↓
Inventory Manager
  ↓
User Data WebSocket
  ↓
REST Reconciliation
  ↓
LIMIT_MAKER Order Engine
  ↓
Partial Fill Handler
  ↓
Cancel / Replace
  ↓
Crash Recovery
  ↓
Kill Switch Execution
  ↓
Testnet Integration Test
  ↓
Long Dry-Run Soak Test
  ↓
baru evaluasi Live
```

Tidak ada:

```text
v3.2.1 → "gas live bro"
```

😭

---

# 🧠 Prinsip project

Urutan prioritas:

```text
Capital Protection
       >
Deterministic Logic
       >
Testability
       >
Execution
       >
Profit
```

Profit penting.

Tapi sistem yang gampang rusak tidak jadi bagus cuma karena pernah profit.

Target kita bukan:

> "Bot yang kelihatan keren."

Target kita:

> **Bot yang kalau kondisi jelek bisa bilang "nggak dulu".**

Itu lebih berguna daripada bot yang tiap 15 menit merasa harus melakukan sesuatu. 🗿

---

# ⚠️ Risiko

Grid trading **bukan passive income otomatis**.

Risiko utama:

- harga breakout keluar range,
- tren kuat,
- volatilitas ekstrem,
- slippage,
- fee berubah,
- order tidak terisi,
- partial fill,
- API/network failure,
- exchange maintenance,
- stale state,
- perbedaan testnet dan live,
- inventory nyangkut ketika market terus bergerak satu arah.

Tidak ada rumus di repository ini yang bisa menghapus risiko market.

Kalau ada yang bilang:

> "Profit pasti."

Simpan screenshot-nya.

Nanti kita taruh di museum. 🗿🏛️

---

# 🔐 FINAL REMINDER

**v3.2.1 BELUM LIVE-TRADING READY.**

Jangan:

```yaml
dry_run: false
```

Jangan kasih:

```text
withdrawal permission
```

Jangan skip:

```bash
pytest -q
```

Dan jangan memaksa grid kalau hasil validasinya jelek.

Kalau grid jelek:

```text
BLOCKED
```

Bukan:

```text
"yaudah gas tipis"
```

---

## 🗿 Adaptive Grid Bot

**Trade less. Validate more. Survive first.**

Versi Gen Alpha:

> **No edge? No trade.  
> No risk control? No cook.  
> Market ngegas? Kita ghosting. 👻📉**
