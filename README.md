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

### Account-risk state (Phase 2B + PATCH 1 F-H1 + F-H2)

Bot membaca saldo Spot base/quote secara read-only dan menilai equity dengan
harga ticker terbaru. Referensi equity (peak / high-water mark) sekarang
**dipersistenkan** di SQLite `bot_state` (key `paper_reference_equity`), jadi
drawdown kill switch tetap berfungsi setelah restart process. Nilai peak
naik mengikuti equity, tidak pernah turun; drawdown dihitung terhadap peak
yang dipersistenkan, bukan terhadap equity saat ini.

Perilaku fail-closed:

- Jika referensi **belum ada** (awal), di-bootstrap dari equity valid pertama
  lalu dipersistenkan.
- Jika referensi **ada tapi rusak** (nilai invalid/corrupt di disk), run
  diblok dengan alasan `EQUITY_REFERENCE_INVALID` — nilai tidak diperbaiki
  diam-diam.
- Drawdown >= `max_equity_drawdown_pct` (default 2%) memblokir submission
  baru via `EQUITY_DRAWDOWN_KILL`.

#### Kill state & cancel-on-kill (F-H2)

Ketika kill trigger menyala (drawdown kill / range-break kill):

1. **Kill state di-latch** ke SQLite persisten (`kill_state`) **sebelum**
   pembatalan, jadi crash di tengah path tetap menyisakan latch aktif.
2. Semua open order **dicoba dibatalkan** oleh `CancelController`.
   - Hasil `CONFIRMED` / `ALREADY_CANCELED` → order lokal jadi `CANCELED`,
     reservasi sisa dilepas.
   - Hasil `UNKNOWN` / `FAILED` → **tidak** dianggap sukses: order tetap
     lokal, reservasi dipertahankan, dan kill state tetap aktif dengan
     `cancel_status=PENDING_RECONCILIATION`.
3. **Tidak ada order pengganti** yang ditempatkan, dan **tidak ada order baru**
   selama kill state aktif (gate di `main()` re-membaca latch sebagai veto
   mutlak).
4. Latch **bertahan melewati restart**: proses baru masuk ulang ke cabang
   kill, mencoba cancel/reconcile lagi, dan stop.
5. Hanya operator yang bisa melepas latch, lewat perintah eksplisit di bawah.

Open-order reconciliation exchange-side belum ada; `open_orders_available_gate`
memblokir plan kalau state open order tidak VERIFIED.

#### Perintah operator (paper/dry-run only)

Keduanya menolak jalan kalau config bukan `dry_run=true` **dan**
`allow_live_execution=false`, dan keduanya menulis audit log persisten.

```bash
# Reset referensi drawdown (eksplisit, tidak pernah otomatis).
# --value <n> set referensi ke n; --clear lupakan (run berikutnya
# re-bootstrap dari equity pertama yang valid).
python scripts/reset_reference_equity.py --db <path> \
    (--value 2000 | --clear) --reason "<kenapa>" [--actor <siapa>]

# Lepaskan kill state. Release TOLAK kalau masih ada open order yang belum
# direconcile; gunakan --reconcile <client_order_id> untuk menegaskan
# tiap order sudah benar-benar batal di exchange.
python scripts/release_kill_state.py --db <path> \
    --reason "<kenapa>" [--actor <siapa>] \
    [--reconcile AG-BNBUSDT-G00001-00000-B ...]

# Laporan status read-only (health/kill/reconciliation/run-state).
# Exit 0=HEALTHY, 1=DEGRADED/KILLED/UNHEALTHY, 2=UNSAFE_CONFIG.
# --json untuk payload mesin; --log <path> juga append ke <path>.jsonl.
python scripts/status_report.py --db <path> [--json] [--log <path>]
```

Audit: `reference_equity_audits` dan `kill_state_audits` menyimpan timestamp,
nilai sebelum/sesudah, alasan, dan actor. Perintah tidak melemahkan kill
switch 2% — setelah release, run berikutnya tetap harus PASS gate risk penuh
sebelum order baru boleh ditempatkan.

#### Observability & operational resilience (Roadmap G)

Lapisan observability read-only, tidak pernah menyentuh logic trading:

- **Structured health report** — `health.collect_health_report` merangkum
  config safety flags, kill latch, reconciliation health, referensi equity,
  jumlah order lokal, pending cancel, keputusan risk terakhir, dan cycle
  terakhir. Payload JSON deterministik (`sort_keys`, tanpa timestamp
  wall-clock), jadi dua state identik menghasilkan byte identik.
- **Status operator command** — `scripts/status_report.py` menampilkan
  rangkuman + exit code sesuai status operasional, dan mengappend JSONL
  machine-readable ke `<log>.jsonl` (file log manusia tetap bersih).
- **Graceful shutdown** — `shutdown.ShutdownCoordinator` hanya flip flag;
  handler sinyal TIDAK pernah menyentuh state. Run-loop cek coordinator di
  batas aman (sebelum paper cycle) sehingga tidak ada kerja separuh. Request
  kedua mempercepat (`forced`); `complete()` terminal.
- **Restart-recovery hardening** — `runstate.verify_restart_safety`
  memverifikasi tiap startup: DB fresh (tanpa activity) = RESUME; ada
  marker COMPLETED = RESUME; marker INTERRUPTED + ada activity =
  RECONCILE_THEN_RESUME; kill latch aktif = KILL_BRANCH; reconciliation
  gagal dengan activity = REFUSE (fail-closed, run tidak planning).
- **Run-state marker** — tiap run persist `last_run_state` (fase, risk
  decision, kill state, jumlah order/pending) supaya restart berikutnya
  bisa verifikasi hand-off yang bersih.

#### Exchange events & reconciliation (Roadmap E)

Lapisan exchange-side yang **read/outcome-only** dan deterministik
(`exchange_events.ExchangeEventApplier`). Memetakan event fill / cancel /
reject / expire ke `PaperOrderEngine` + `CancelController`. **Tidak ada**
jalur placement order, **tidak ada** release kill latch, dan **tidak ada**
implementasi live stream — reconciler-nya adalah abstraksi (seam) yang
masih kosong, jadi tidak ada kode live yang bisa kehabili.

Sifat deterministik yang dites:

- **Event duplikat** — `event_id` yang sudah tercatat = no-op idempoten.
- **Out-of-order / gap** — seq <= watermark = OUT_OF_ORDER; seq >
  watermark+1 = SEQUENCE_GAP. Keduanya TIDAK di-apply dan menandai
  reconciliation REST diperlukan. Watermark monotonic.
- **Partial / full fill** — via `apply_fill`; fill melebihi qty tersisa
  ditolak engine dan ditandai reconcile (tidak dipaksa).
- **Cancel / already-canceled** — order terminal = no-op bersih.
- **Unknown order / unknown state** — dicatat, TIDAK di-apply, ditandai
  reconcile; tidak pernah inventing exposure lokal.
- **Stale local state / network failure** — snapshot REST yang authoritative
  lewat seam read-only; snapshot tidak dapat dicapai = fail-closed (state
  lokal tidak disentuh, watermark tidak di-reset).
- **Restart recovery** — watermark + log event persist di SQLite
  (`exchange_sequence`, `exchange_events`); proses baru lanjut dari
  watermark dan tidak apply ulang event yang sudah dilihat.
- **Convergence** — apply ulang event set yang sama, dan reconcile ulang
  dari snapshot yang sama, menghasilkan state lokal yang identik.
- **Interaksi kill-state** — applier tidak pernah placement order dan
  tidak release kill latch; saat kill aktif, applier hanya mencatat
  cancel/fill agar jalur release operator melihat pending set yang akurat.

Sebelum implementasi live user-data stream diizinkan: model deterministik +
test adalah deliverable Roadmap E. Live feed = tugas terpisah,
berizin eksplisit.

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

### 4. Stop batas bawah candle 15m (lower-boundary kill)

Default:

```yaml
stop_if_below_lower_pct: 0.02
```

Ini pengaman **berbeda** dari range-break buffer di atas. Yang dicek bukan
harga ticker, melainkan **close candle 15m yang sudah closed**:

```text
threshold = LOWER_PRICE × (1 - 0.02)
close_15m  <= threshold  →  KILL
```

Contoh: LOWER_PRICE = 94 → threshold = 92.12.
Kalau candle 15m terakhir close di 92.00 atau di bawahnya,
meskipun ticker masih di dalam range:

```text
KILL: LOWER_BOUNDARY_STOP_15M
   ↓
kill state di-latch (persist, tahan restart)
   ↓
cancel open orders (fail-closed, UNKNOWN/FAILED = tetap aktif)
   ↓
TIDAK ada order baru, TIDAK ada order pengganti
```

Aturan:

- Hanya pakai candle **closed** (candle yang masih berjalan di-drop;
  ticker tidak boleh menggantikan close candle).
- Fail-closed: data candle hilang/rusak/NaN → veto (`DATA_UNAVAILABLE`),
  config invalid → veto (`CONFIG_INVALID`), tidak pernah PASS diam-diam.
  Kedua veto itu memblokir order untuk run itu saja (tidak mengunci kill
  state); hanya close yang terkonfirmasi di bawah threshold yang mengunci.
- `stop_if_below_lower_pct` **wajib** ada di config (divalidasi: Decimal
  finite di (0,1)). Tidak ada fallback tersembunyi.
- Range-break kill (butir 2) tidak diubah; kedua pengaman berjalan
  terpisah.

### 5. Market filter

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
1031 passed
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

# 🧭 Phase 6A: Binance Spot Testnet Read-Only Adapter

**Purpose:** Connect the existing paper/risk architecture to Binance Spot Testnet for READ-ONLY market/account/order-state verification.

**This is NOT live trading.**

## Setup

1. Create Binance Spot Testnet API credentials (separate from production).
2. Put them in local `.env`.
3. Confirm testnet environment (`BINANCE_ENV=testnet`).
4. Run the readonly connectivity check.
5. Run unit tests.

## Safety guarantees

- **Testnet only.** Production URLs are rejected at construction time.
- **Read-only.** No order placement, cancellation, modification, batch orders, or OCO.
- **Fail-closed.** If environment is missing, not exactly `testnet`, base URL is production, or safety controls (`DRY_RUN=true`, `ALLOW_LIVE_EXECUTION=false`) are violated, the adapter raises a configuration error immediately.
- **Credentials externalized.** API keys come from environment variables only. Never hardcoded. Never logged. Never printed.
- **Decimal financial values.** Prices, quantities, balances, and fees are always `Decimal`.
- **API failure = fail-closed.** No fake success, no fallback to cached/candle prices.

## Commands

```bash
python scripts/testnet_readonly_check.py
```

```bash
pytest tests/test_binance_testnet.py -q
```

## Explicit disclaimers

- Testnet credentials are separate from production credentials. **Do not reuse production secrets.**
- API key must not have withdrawal permission.
- **Phase 6A does not submit orders.**
- **Phase 6A does not cancel orders.**
- **Phase 6A does not touch futures or margin.**
- **Phase 6A is not live trading.**

---

# 🧭 Phase 7: Gated Testnet Order Path (LIMIT_MAKER + cancel)

**Purpose:** Close the Round 5 testnet blockers — a real, explicitly gated
order path on Binance Spot **Testnet** plus the concrete REST reconciliation
executor, verified end-to-end against the live testnet.

**This is NOT live trading.** Live/production execution remains structurally
impossible: `main()` still refuses to run with `dry_run=false`, no production
endpoint can be constructed, and the order path cannot reach any non-testnet
host.

## What was added

- **`testnet_orders.py`** — `BinanceTestnetOrderClient`:
  - Only two capabilities exist: `place_limit_maker_order` (post-only,
    maker-only, exact decimal strings on the wire) and
    `cancel_order_by_client_id`.  No market/OCO/algo/SOR/batch/withdraw
    methods exist on the class.
  - Double-gated: a validated testnet config (testnet URL + `DRY_RUN=true` +
    `ALLOW_LIVE_EXECUTION=false`) AND the explicit env gate
    `TESTNET_ORDERS_ENABLED=true` (strict boolean, default **false**).
  - Deterministic outcomes: validated ack (CONFIRMED), typed rejection with
    the Binance error code (deterministic FAILED — the order was not
    accepted), or typed UNKNOWN (timeout/network/rate-limit).  A POST is
    never retried; a lost submission ack is settled by resolving the
    deterministic clientOrderId, never by resubmitting.
  - The ambiguous cancel family (`-2011`/`-2013` "unknown order") is never
    reported as a confirmed cancel: `make_cancel_executor(resolver=...)`
    settles it only via an authoritative re-query returning `CANCELED`
    (a fill racing the cancel stays UNRECONCILED — it became inventory).
- **`scripts/testnet_order_path_check.py`** — the §21 verification harness:
  - Read-only by default (connectivity, clock skew, filters, balances).
  - `--place-order` (requires `TESTNET_ORDERS_ENABLED=true`) runs the full
    path: place → authoritative resolve → open-order reconcile →
    duplicate-clientOrderId rejection (while open) → verified cancel →
    post-cancel re-resolve → restart recovery (fresh client instances).
  - `--verify-cid <id>` resolves a prior order from a brand-new process
    (true restart-recovery proof).
  - Fail-closed everywhere; exit 0 only if ALL checks pass; best-effort
    cleanup so a failed run leaves no open testnet order behind.

## Verified on Binance Spot Testnet (2026-10-03, BNBUSDT)

```text
connectivity / clock skew / filters / balances   PASS
place LIMIT_MAKER BUY 0.01 BNB @ 764.62          PASS  (status NEW)
authoritative resolve by clientOrderId           PASS  (NEW, orderId echoed)
open-order reconciliation (exact match)          PASS
duplicate clientOrderId prevention               PASS  (rejected, code -2010)
verified cancellation                            PASS  (settled CANCELED via
                                                  authoritative re-query)
post-cancel re-resolve                           PASS  (CANCELED)
restart recovery (fresh process)                 PASS  (CANCELED, both cids)
main() dry-run cycle vs real testnet data        PASS  (all risk gates vetoed
                                                  the current grid, no order)
```

Testnet note: the testnet repeatedly "loses" the first cancel response and
answers the (SDK-retried) cancel with `-2011` — the executor's
resolve-to-settle path exists precisely for this and is exercised live.

## Operator commands

```bash
# Read-only verification (no orders).
python scripts/testnet_order_path_check.py

# Full order-path verification (TESTNET ORDERS, gated).
TESTNET_ORDERS_ENABLED=true python scripts/testnet_order_path_check.py --place-order

# Restart-recovery proof for a prior order (fresh process).
python scripts/testnet_order_path_check.py --verify-cid AGTV-BNBUSDT-...
```

`TESTNET_ORDERS_ENABLED` defaults to **false**; anything other than strict
`true`/`false` is a configuration error.  Live trading remains disabled.

---

# 🧭 Phase 8: Bounded Continuous Testnet Cycle

**Purpose:** Repeat the verified Round 7 order path as a *bounded,
controlled cycle* — market data → indicators → range → grid → risk veto →
order intents → LIMIT_MAKER placement (testnet only) → authoritative
reconciliation → verified cancellation → cleanup proof.

**This is NOT live trading and NOT a 24/7 loop.**  Cycles are bounded
(1–100 per run), each cycle ends flat (verified cancel of still-open
orders), and the production `main()` cycle remains paper-only.

## What was added

- **`testnet_cycle.py`** — `TestnetCycleRunner` + `CycleLedger`:
  - Reuses the EXACT production math read-only (`indicators`, `auto_range`,
    `build_geometric_grid`, `validate_quantized_order_plan`) and the EXACT
    `risk_engine` gates: range-break kill (±1%), 15m candle-close
    lower-boundary kill, 2% equity-drawdown kill against a persisted
    high-water reference, market filter (ADX/ATR/BB/volume), open-order
    capacity, per-cell minimum net profit (0.30%), and the strict
    price-inside-range gate.  The Risk Engine remains the authoritative
    veto — nothing is re-implemented or bypassed.
  - Separate SQLite ledger (`data/testnet_cycle.sqlite3`, user_version 800;
    the paper DB is never touched or migrated): runs, orders (state machine
    `INTENT → SUBMITTED_UNKNOWN → OPEN/PARTIALLY_FILLED →
    FILLED/CANCELED/REJECTED`, plus `PENDING_RECONCILIATION`), an event
    journal (full observability), the persistent kill latch, and the
    reference-equity high-water mark.
  - Restart recovery: a fresh process reconciles every non-terminal ledger
    order against the exchange BEFORE new cycles; anything unresolvable
    stays `PENDING_RECONCILIATION` and blocks placement (fail closed).
  - Kill path: latch persists FIRST, then fail-closed cancel-on-kill;
    restart enters the kill branch (no new orders, recovery/cleanup only).
  - Deterministic clientOrderIds (`AGTC-<SYMBOL>-<run>-<cycle>-<seq>`,
    never reused); lost submission acks are resolved by clientOrderId and
    never resubmitted; ambiguous cancels settle only via authoritative
    re-query (§5).
  - Cleanup proof: reconcile → cancel only confirmed-open own orders →
    re-resolve → PROVE zero own open orders and zero non-terminal ledger
    orders; anything unprovable FAILS with the exact ids.  Foreign orders
    (other namespaces) are never touched; their presence refuses placement.
- **`scripts/testnet_cycle_check.py`** — gated CLI:
  - `--mode rehearsal` (default): full cycle, zero writes.
  - `--mode orders`: requires `TESTNET_ORDERS_ENABLED=true`; bounded cycles
    with real testnet LIMIT_MAKER orders (per-order quote size from config,
    default 25 USDT), each cycle ends flat.
  - `--cleanup-only`, `--status`; SIGINT/SIGTERM stop at safe boundaries;
    exit 0 only when the run completed AND cleanup proved zero open orders.

## Verified on Binance Spot Testnet (2026-10-03)

```text
rehearsal cycle (BNBUSDT, read-only)      PASS  (range veto recorded, exit 0)
orders cycle (SOLUSDT, 2 cycles)          PASS  (4 real orders placed, all
                                                canceled verified, cleanup ok)
restart run (fresh process, same ledger)  PASS  (recovered, 2 more orders,
                                                cleanup ok)
cleanup-only proof                        PASS  (0 own open orders, 0 unresolved)
symbol probes                             ETH/BTC vetoed (width/quality),
                                                DOGE vetoed (volume) — gates
                                                authoritative on live data
```

## Operator commands

```bash
# Read-only rehearsal (no gate needed, no orders).
python scripts/testnet_cycle_check.py --mode rehearsal --cycles 2

# Bounded orders cycle (requires TESTNET_ORDERS_ENABLED=true).
TESTNET_ORDERS_ENABLED=true python scripts/testnet_cycle_check.py \
    --mode orders --symbol SOLUSDT --cycles 2 --interval 5

# End-of-run cleanup proof / status.
TESTNET_ORDERS_ENABLED=true python scripts/testnet_cycle_check.py \
    --mode orders --cleanup-only
python scripts/testnet_cycle_check.py --status
```

Live trading remains disabled.  `DRY_RUN=true`, `ALLOW_LIVE_EXECUTION=false`,
`main()` paper-only — unchanged.

---

# 🧭 Phase 9: Economics — Fees, Partial Fills, Realized PnL

**Purpose:** close the accounting loop on the testnet cycle: prove the
executable grid economics end-to-end, account for partial fills from
authoritative execution data, and track realized PnL deterministically.

## Grid economics (theoretical AND executable)

Every candidate grid reports BOTH nets, and the executable one is the gate:

- **Theoretical net** — from the raw 0.60% grid step:
  `net_pct_from_step(step, maker, maker, slippage)` (0.60% step with
  0.10%/leg conservative fees + 0.05% round-trip slippage ⇒ ≈0.349%).
- **Executable net** — the worst grid cell AFTER tick/step quantization
  (`validate_quantized_order_plan`), re-checked against
  `hard_min_net_pct = 0.003`.  A marginal cell is rejected
  (`NET_PROFIT_BELOW_HARD_MIN_AFTER_QUANTIZATION`) — never rounded upward
  to pass.  Fee changes, slippage changes, coarse tick sizes, quantity
  rounding, and minNotional violations each fail the plan fail-closed.

## Authoritative fills → inventory → realized PnL (`economics.py`)

- The adapter now validates and returns `cummulativeQuoteQty` from order
  payloads, giving the AUTHORITATIVE average execution price
  (`cumQuote / executedQty`); without it the limit price is used and the
  fill is explicitly `ESTIMATED_PRICE`.
- `testnet_cycle` syncs the executed-quantity DELTA of every order on each
  authoritative ack/resolve — requested quantity is never assumed to be
  filled quantity.  Deltas are priced exactly
  (`(newCum − recordedCum) / deltaQty`), persisted as append-only ledger
  fill rows (replay-safe unique key), and applied to `CycleEconomics`.
- `CycleEconomics` (average-cost): BUY fills grow the position by exactly
  the executed amount (fee included in basis); SELL closes basis at the
  average cost.  Realized PnL = proceeds − basis − BOTH fees — a grid is
  profitable only AFTER fees (`sell > buy` alone proves nothing).  A SELL
  exceeding the held position raises: shorting is structurally impossible
  at the accounting layer.
- Restart reconstruction: a fresh process replays the ledger's fill rows
  in order and reproduces the exact pre-restart state
  (`CycleEconomics.replay`).  Partial-then-cancel keeps the executed
  portion; the unfilled remainder releases only on the authoritative
  terminal state.
- Fees use the repo-wide conservative fallback maker rate applied to
  executed notional; per-trade commissions (get_my_trades) are a future
  refinement that must only ever REPLACE the estimate with authoritative
  data.

## Bounded testnet validation

`python scripts/testnet_cycle_check.py --validate --cycles 60 --symbol SOLUSDT`
(requires `TESTNET_ORDERS_ENABLED=true`): bounded cycles (hard cap 100),
Risk-Engine vetoes recorded and skipped safely (orders are never forced),
every order tracked + reconciled, and the final report includes cycles,
orders created / filled / partially filled / canceled / UNKNOWN-PENDING,
theoretical + executable net, realized PnL, fees, and the fail-closed
zero-open-order cleanup proof.

---

# 🚀 Continuous Runtime (testnet/paper daemon)

`runtime.py` is the long-running production wrapper for the VPS: it
repeatedly invokes the **existing authoritative single-cycle entrypoint**
(`main.main`) — it contains no trading logic of its own, places no orders,
and never bypasses the Risk Engine, kill switch, reconciliation, or any
safety gate.

## How scheduling works

- One cycle per **new closed candle** of the configured `timeframe`
  (15m — deterministic wall-clock boundaries, no new timing model), with a
  configurable grace period (`runtime.boundary_grace_seconds`) after the
  boundary.
- `runtime.interval_seconds` (default 60) paces wake-ups; after a cycle
  that refused (config error / restart-reconciliation refusal) the daemon
  keeps monitoring at that interval.  Within one closed candle it never
  re-invokes just because time passed — the cycle itself is idempotent per
  closed candle (deterministic `cycle_id` replay), so a restart cannot
  create duplicate orders.
- A normal **BLOCK** decision is a healthy outcome: the daemon logs it and
  keeps monitoring.  An unexpected exception is logged with a traceback
  and **fails closed** — the process exits nonzero so systemd restarts it
  fresh; nothing is retried or invented at the runtime layer.

## Running manually (testnet/dry-run)

```bash
# continuous run (Ctrl-C for graceful shutdown)
python runtime.py

# bounded observation (exits after N cycles)
python runtime.py --max-cycles 2
```

The runtime refuses to start unless `environment.dry_run=true`;
`allow_live_execution` must stay false.  Structured events are logged as
`RUNTIME START / CYCLE START / CYCLE RESULT / CYCLE BLOCKED / RUNTIME WAIT /
RUNTIME STOP / CYCLE UNEXPECTED EXCEPTION` to stdout and
`logs/grid_bot.log` (the cycle's own logging is unchanged).

## systemd (VPS)

A unit template ships in `deploy/adaptive-grid.service`:

```bash
sudo cp deploy/adaptive-grid.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adaptive-grid.service   # start + enable
systemctl status adaptive-grid.service              # health
journalctl -u adaptive-grid.service -f              # live logs
sudo systemctl stop adaptive-grid.service           # graceful SIGTERM stop
```

The unit runs as a dedicated non-root `adaptive-grid` user from
`/opt/adaptive-grid` with `/opt/adaptive-grid/.venv/bin/python`, uses
`network-online.target`, and `Restart=on-failure` — a graceful stop is not
restarted, only an actual process failure is.

---

# 🗂️ Struktur project

```text
adaptive-grid/
├── main.py
├── config.yaml
├── config_loader.py
├── market_data.py
├── binance_testnet.py
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
├── scripts/
│   └── testnet_readonly_check.py
└── tests/
    ├── test_config.py
    ├── test_grid.py
    ├── test_indicators.py
    ├── test_profit.py
    ├── test_range.py
    ├── test_risk.py
    ├── test_storage.py
    ├── test_symbol_rules.py
    ├── test_fee.py
    └── test_binance_testnet.py
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
