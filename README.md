# adaptive-grid

Bot grid **Binance Spot** konservatif multi-simbol dengan gerbang masuk/keluar berbasis indikator ketat, validasi ekonomi grid executable, kill switch drawdown global 2%, dan dashboard read-only.

Ada dua mode eksekusi non-live: **PAPER** (simulasi internal; modal sesi berasal dari saldo USDT Binance testnet; tidak ada order yang pernah mencapai Binance) dan **TESTNET EXECUTION** (order Spot **Testnet Binance** asli pada dana virtual). LIVE dinonaktifkan oleh setiap default dan memerlukan keempat gerbang: `EXECUTION_MODE=live`, `BINANCE_ENV=live`, `DRY_RUN=false`, `ALLOW_LIVE_EXECUTION=true`.

---

## Tujuan

Bot menunggu kondisi pasar tenang dan oversold, menempatkan grid kecil order limit buy post-only di bawah harga, menjual setiap buy yang terisi satu langkah grid lebih tinggi, dan keluar sepenuhnya saat kondisi berubah menjadi hostil. Proteksi modal mendominasi setiap kepentingan lain:

- **Trade Binance Spot saja** — tanpa futures, margin, leverage, shorting, martingale, atau averaging agresif.
- Masuk hanya di bawah kondisi ketat; keluar begitu kondisi memburuk.
- Tidak pernah menempatkan grid yang keuntungan net executable-nya tidak bisa melampaui minimum.
- Hentikan semuanya pada drawdown equity global 2%.

## Batasan keamanan (baca dulu)

- **Tidak ada jaminan profitabilitas.** Tidak ada yang di sini yang menjanjikan keuntungan. Gerbang ekonomi hanya mencegah grid yang *diketahui tidak menguntungkan*.
- **Testnet adalah environment default.** Harga dan likuiditas testnet berbeda dari produksi; sinyal yang dihitung pada data testnet hanya untuk validasi plumbing, bukan bukti performa.
- **Equity berbasis PnL**, diankor di `START_EQUITY`:
  `equity = START_EQUITY + realized_pnl - fees + unrealized_pnl`. Ini **bukan** rekonsiliasi saldo akun penuh. Kill drawdown 2% mengukur kerugian basis modal simulasi.
- **Kill state tidak auto-reset.** Global kill atau simbol `STOPPED` oleh gerbang batas-bawah 15m tetap ada sampai operator intervensi (edit database state atau database baru).
- Gerbang batas-bawah 15m **fail-closed**: data 15m yang hilang atau tidak valid memblokir order baru untuk simbol terkait, bukan menebak.
- Rekonsiliasi fill mode live diimplementasikan tapi belum diuji melawan kondisi produksi; anggap mode live sebagai belum terbukti.

## Arsitektur

```
adaptive-grid/
├── .env.example      # template konfigurasi (.env adalah SUMBER KONFIGURASI TUNGGAL)
├── config.py         # load + validasi .env -> satu objek Config immutable
├── indicators.py     # ADX/RSI/BB %B/VO/Z-score/ATR deterministik (hanya candle closed)
├── strategy.py       # gerbang masuk ketat, gerbang keluar, prioritas keluar, cooldown
├── grid.py           # konstruksi grid + ekonomi executable (sadar quantization)
├── risk.py           # veto order, kill drawdown 2%, gerbang batas-bawah 15m
├── exchange.py       # Binance Spot REST (testnet default), dry-run + live executor
├── state.py          # database SQLite tunggal (state, order, fill, PnL, kills)
├── bot.py            # loop runtime mengorkestrasi siklus penuh
├── dashboard.py      # dashboard HTTP read-only di atas database state
└── tests/            # test suite deterministik fokus (offline)
```

**Tidak ada konfigurasi YAML, lapisan kompatibilitas, atau duplikasi sumber konfigurasi.** `.env` adalah sumber kebenaran tunggal; modul lain menerima objek `Config` tervalidasi dan tidak pernah membaca environment sendiri.

## Konfigurasi

Salin `.env.example` ke `.env` dan isi. Startup gagal dengan error jelas yang mencantumkan setiap kunci yang hilang atau tidak valid — tidak ada fallback diam untuk nilai strategi.

Grup kunci:

| Grup | Kunci |
|---|---|
| Environment & safety | `BINANCE_ENV` (`testnet`/`live`), `EXECUTION_MODE` (`paper`/`testnet`/`live`), `DRY_RUN`, `ALLOW_LIVE_EXECUTION` |
| Scope pasar | `PAIR_LIST` (cth `BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT`), `INDICATOR_TIMEFRAME` |
| Indikator | `ADX_PERIOD`, `RSI_PERIOD`, `BB_PERIOD`, `BB_STD`, `VO_FAST`, `VO_SLOW`, `ZSCORE_PERIOD`, `ATR_PERIOD` |
| Gerbang masuk (SEMUA harus terpenuhi) | `ENTRY_ADX_MAX`, `ENTRY_RSI_MAX`, `ENTRY_VOLUME_OSC_MIN`, `ENTRY_BB_PERCENT_B_MAX` |
| Gerbang keluar (SALAH SATU memicu) | `EXIT_RSI_MIN`, `EXIT_ADX_MIN`, `EXIT_BB_PERCENT_B_MIN`, `EXIT_ZSCORE_ABS_MAX` |
| Ekonomi grid | `GRID_STEP_ATR_MULTIPLIER`, `GRID_GROSS_MIN`, `MIN_NET_PROFIT_PER_GRID`, `MAKER_FEE`, `TAKER_FEE`, `SLIPPAGE_ESTIMATE` |
| Risiko | `MAX_DRAWDOWN_PERCENT` (hard cap 2), `STOP_IF_BELOW_LOWER_PERCENT` (hard cap 2), `START_EQUITY`, `COOLDOWN_HOURS` |
| Kredensial | `BINANCE_TESTNET_API_KEY/SECRET` (testnet), `BINANCE_API_KEY/SECRET` (produksi; tidak digunakan kecuali live sepenuhnya digerbang) |

Hard floor yang divalidasi: `GRID_GROSS_MIN >= 0.005` (0.50%), `MIN_NET_PROFIT_PER_GRID >= 0.002` (0.20%), `MAX_DRAWDOWN_PERCENT <= 2`, `STOP_IF_BELOW_LOWER_PERCENT <= 2`.

### Mode eksekusi

| `BINANCE_ENV` | `EXECUTION_MODE` | Perilaku |
|---|---|---|
| `testnet` | `paper` (default) | Simulasi internal; modal sesi dari saldo USDT testnet saat pembuatan sesi; **tidak ada order Binance yang pernah dikirim**; transaksi paper tidak pernah menyentuh akun exchange. |
| `testnet` | `testnet` | Order **Spot Testnet Binance** asli dengan kredensial testnet saja; butuh `DRY_RUN=false`; exchange adalah sumber kebenaran untuk fill, fee, dan inventory. |
| `live` | `live` | Eksekusi produksi — terkunci kecuali `DRY_RUN=false` **dan** `ALLOW_LIVE_EXECUTION=true` **dan** live key ada. |

Konsistensi dipaksa saat startup (fail closed): `EXECUTION_MODE=live` butuh `BINANCE_ENV=live` dan sebaliknya; `EXECUTION_MODE=testnet` butuh `DRY_RUN=false` dan kredensial testnet; `EXECUTION_MODE=testnet` tidak pernah menyentuh endpoint produksi (base URL dipilih gerbang, bukan key).

### Modal sesi (paper/testnet)

Di start pertama bot membuat **sesi eksekusi**: modal sesi berasal dari `START_EQUITY` (jika > 0) atau dari saldo USDT Binance testnet (butuh kredensial testnet; fail-closed jika tidak tersedia atau nol). Equity = `modal sesi + realized PnL - fees + unrealized PnL`; saldo wallet hanyalah telemetry display dan tidak pernah diimpor ulang ke ledger paper. Sesi persist: restart melanjutkan modal yang sama, session id, dan baseline drawdown. Mengganti `EXECUTION_MODE`/`BINANCE_ENV` melawan database sesi yang ada menolak startup; escape hatch deterministiknya adalah reset eksplisit:

```bash
python bot.py --reset-session    # menolak saat ada order terbuka; hapus sesi + kill state
```

### Gerbang live-trading

Endpoint live dan kredensial live digunakan **hanya** saat **ketiganya** terpenuhi:

1. `DRY_RUN=false`
2. `ALLOW_LIVE_EXECUTION=true`
3. `BINANCE_ENV=live`

Kombinasi lain tetap di testnet (atau menolak start — cth `live` environment dengan `DRY_RUN=false` tapi gerbang tertutup ditolak saat startup). `DRY_RUN=true` (default) tidak pernah mengirim order ke Binance.

## Aturan Strategi

**Masuk** (simbol boleh memulai grid hanya saat SEMUA terpenuhi, pada candle CLOSED `INDICATOR_TIMEFRAME`):

- ADX(14) < 20
- RSI(14) < 35
- Volume Oscillator(5,10) > 0
- Bollinger %B(20,2) <= 0

Riwayat candle tidak cukup artinya **TIDAK TRADE** — tidak pernah exception, tidak pernah nilai fabricated.

**Keluar** (grid aktif keluar saat SALAH SATU terpenuhi; keluar prioritas atas masuk):

- RSI(14) >= 70
- ADX(14) > 25
- Bollinger %B > 1
- abs(Z-Score(20)) > 2.5

Keluar otomatis: hentikan order baru → batalkan semua order terbuka → **verifikasi** → market-sell inventory yang dipegang → **verifikasi** → catat alasan keluar, realized PnL, dan fee → cooldown (`COOLDOWN_HOURS`, default 3j) → tidak ada re-entry otomatis selama cooldown. Verifikasi gagal apa pun adalah fail-closed: simbol berhenti di `ERROR`.

Setelah keluar otomatis, simbol tidak bisa memulai grid baru sampai cooldown habis. Cooldown survive restart proses.

Perintah `--check-grid` read-only memvalidasi grid dan ekonomi net executable-nya untuk setiap simbol terkonfigurasi sebelum eksekusi:

```bash
python bot.py --check-grid
```

Ia memakai path grid-building produksi persis (`MarketData` + `grid.build_grid`): mengambil filter exchange, harga rata-rata terbobot reference, dan ATR candle closed, membangun plan dengan logika quantization/fee/slippage/PERCENT_PRICE_BY_SIDE yang sama dengan eksekusi live, lalu mencetak hasil ACCEPTED/REJECTED per simbol dengan min/max/average executable net profit dan level di bawah minimum required. Ia submit, cancel, dan modify apapun tidak — tidak pernah buka database state; exit non-zero jika simbol terkonfigurasi apapun gagal gerbang minimum net-profit.

## Ekonomi Grid

- Grid step = `GRID_STEP_ATR_MULTIPLIER × ATR(14)` (default 1.0).
- Grid arithmetic untuk BTC/ETH/BNB, geometric untuk SOL (mapping di `config.py`).
- Harga buy dibulatkan **ke bawah** dan sell **ke atas** ke tick size exchange; qty menghormati step size dan minimum notional.
- **Ekonomi executable** (sesudah quantization exchange, dengan fee buy, fee sell, dan estimasi slippage kedua sisi) adalah yang otoritatif.
- Grid **diblokir** kecuali executable gross >= 0.50% dan executable net >= 0.20% (`GRID_GROSS_MIN`, `MIN_NET_PROFIT_PER_GRID`). Grid buruk tidak pernah diperlebar atau dipaksa.
- **PERCENT_PRICE_BY_SIDE di-parse dan di-enforce lokal.** Untuk setiap simbol, harga rata-rata terbobot exchange (`GET /api/v3/avgPrice`, lewat filter `avgPriceMins` — **tidak diasumsikan** sama dengan last price) adalah reference: level BUY harus di dalam `reference × [bidMultiplierDown, bidMultiplierUp]` dan harga SELL di dalam `reference × [askMultiplierDown, askMultiplierUp]`. Level grid yang melanggar band di-drop dari plan (atau grid diblokir saat tidak ada yang tersisa); child sell di luar band di-defer ke siklus berikutnya terhadap reference saat itu, bukan submit order yang akan ditolak Binance. Penolakan definitif (HTTP 400 filter failure, kode -1013/-2010) menghentikan simbol di `ERROR` — tidak pernah di-retry buta.

## Aturan Risiko

- **Global drawdown kill switch: 2%** (`MAX_DRAWDOWN_PERCENT`, hard cap). Drawdown diukur terhadap high-water mark `equity = START_EQUITY + realized - fees + unrealized`. Saat breach: kill **di-latch dan persist dulu** (tidak ada order baru global), lalu per simbol semua order dibatalkan & diverifikasi, dan semua inventory dilikuidasi & diverifikasi. Kegagalan verifikasi cancel atau liquidasi adalah fail-closed (simbol `ERROR` + risk event) tapi **tidak pernah hapus kill** — kill tetap latch di restart sampai operator intervensi.
- **Proteksi batas-bawah 15m** (`STOP_IF_BELOW_LOWER_PERCENT`, hard cap 2%): jika **CLOSE 15m candle terakhir** (bukan intrabar wick) paling banyak `lower × (1 - 2%)`, simbol dihentikan (order dibatalkan, inventory dilikuidasi, state `STOPPED`). Data 15m hilang/tidak valid memblokir order baru untuk simbol itu (fail-closed) bukan memicu atau menebak.
- Risk engine punya **wewenang veto atas setiap order**: global kill atau simbol risk-stopped veto semua placement.
- Order pakai LIMIT_MAKER (post-only) di mana didukung, bawa client order id unik, dipersist sebelum submit, dan **tidak pernah di-retry buta**: setelah kegagalan network order direkonsiliasi by client id; state final unknown memicu kondisi fail-closed dan hentikan simbol.
- **Akuntansi fill idempotent per trade exchange**: trade id adalah kunci idempotensi; satu transaksi atomis catat fill dan update inventory, average cost, dan realized PnL. Partial fill dihitung saat terjadi, pakai qty executed aktual — bukan qty planned. Rekonsiliasi berulang atau restart tidak pernah double-count.
- BUY yang terisi mengkonversi **qty aktual yang diperoleh** jadi child SELL order (dilacak per order di `child_sell_qty`, diupdate atomis dengan pembuatan child) — duplicate child sell strukturnya mustahil.
- Likuidasi pakai satu client order id per attempt (tracked end-to-end), rekonsiliasi id eksak setelah error network, hitung sisa dari qty executed aktual, lalu verifikasi hasil melawan **saldo akun base asset otoritatif** (testnet/live). Kegagalan verifikasi atau sisa inventory berarti: TIDAK dilikuidasi, simbol fail closed.

## State

Satu database SQLite (`state.db` default, path via `--db`) menampung state global bot, per-simbol state, cooldown, nilai plan grid, order, fill, fee, realized PnL, risk event, dan kill state. Schema bawa version stamp (`schema_version` di `meta`), diterapkan migrasi deterministik minimal saat startup. Tidak ada tabel kompatibilitas untuk arsitektur sebelumnya.

## Dashboard

```bash
python dashboard.py --db state.db --host 127.0.0.1 --port 8080
```

Konsol operator retrofuturistik read-only ("sistem kontrol trading crypto serius dari 1987 alternatif"): display CRT near-black dengan grid teknis halus dan scanlines, warna aksen fosfor, tipografi telemetry monospace, panel KPI bergaya instrument, modul telemetry per-simbol untuk setiap pair terkonfigurasi, dan chart net-PnL bergaya oscilloscope. Auto-refresh dari API tiap 5 detik tanpa reload halaman; saat gagal feed tampil `DATA STALE` dan pertahankan nilai baik terakhir — tidak ada yang difabrikasi; `DATA LIVE` kembali saat API pulih. Environment (`TESTNET`/`LIVE`) dan execution mode (`PAPER`/`TESTNET`/`LIVE`) di header adalah record persist runtime sendiri — tidak bisa di-set/ubah dari dashboard.

Routes (GET only): `GET /` (shell console — static HTML/CSS/JS, no external assets), `GET /api/state` (JSON snapshot), `GET /api/history` (telemetry net PnL kumulatif dari fill ledger; kosong saat tidak ada fill — history tidak pernah diinvent). Semua method write return `405`. Console tidak implement logic strategi, place/cancel apapun, expose kredensial, tidak pernah baca `.env` atau environment variable, dan render setiap nilai dynamic lewat safe DOM API (`textContent`).

Data tampil: equity global, reference equity, drawdown, max drawdown, open orders, realized PnL, fees, kill switch state/reason, runtime dan database status; per simbol: state, risk status, price, timeframe, entry status/blocker, exit status/reason, cooldown, grid mode/step/count, gross & net per grid, inventory, average cost, open orders, realized PnL, fees.

State simbol ditampilkan verbatim dari database — tidak pernah di-infer: `WAITING`, `ENTRY_BLOCKED`, `GRID_BLOCKED`, `ACTIVE`, `COOLDOWN`, `EXITING`, `STOPPED`, `KILL_ACTIVE`, `ERROR`. Jika database tidak tersedia console tetap up dan laporkan `DATABASE UNAVAILABLE` bukan crash.

## Instalasi

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # lalu edit .env
```

Python 3.10+ direkomendasikan. Dependency runtime minimal: `python-dotenv` (plus `pytest` untuk test suite).

## Menjalankan (dry-run)

```bash
python bot.py --once          # satu siklus
python bot.py                 # loop (30s siklus); mode PAPER default
```

Startup log environment sebagai `TESTNET` atau `LIVE` (bukan kredensial) dan menolak start pada masalah konfigurasi apapun.

### Setup Testnet

1. Buat API key di <https://testnet.binance.vision/> — **hanya izin Spot trading; withdrawal dinonaktifkan.**
2. Masukkan ke `.env` di `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`.
3. Mode PAPER (default) pakai key ini read-only untuk dapatkan modal sesi; tidak ada yang pernah di-submit.
4. Untuk eksekusi order testnet asli, set `EXECUTION_MODE=testnet` dan `DRY_RUN=false`. Validasi path dulu:

```bash
python bot.py --check-exchange                       # konektivitas/auth/filter (read-only)
python bot.py --check-grid                           # validasi konstruksi grid + ekonomi net executable (read-only, no order)
python bot.py --testnet-order-selftest BTC/USDT      # place + verifikasi + cancel satu order jauh dari market
python bot.py                                        # jalankan eksekusi testnet
```

Order self-test adalah flag-gated dan tidak pernah jalan otomatis; ia place LIMIT_MAKER buy 50% di bawah market dan cancel — tidak bisa fill.

## Deployment (VPS / systemd)

Template deploy ada di `deploy/`. Kedua service jalan sebagai user `adaptive-grid` di `/opt/adaptive-grid` dan share **satu database otoritatif**: `/opt/adaptive-grid/state.db`. Runtime trading punya semua write; dashboard buka read-only. Tidak ada database state kedua — unit lama yang nunjuk database legacy di `data/` sudah obsolete dan harus diganti.

Fresh deploy (VPS existing simpan `state.db` dan `.env`):

```bash
# 1. clone atau update repository
sudo -u adaptive-grid git -C /opt/adaptive-grid pull --ff-only origin main
# (mesin baru: sudo git clone https://github.com/rmdnl/adaptive-grid.git /opt/adaptive-grid)

# 2. buat/refresh virtualenv
cd /opt/adaptive-grid && sudo -u adaptive-grid python3 -m venv .venv

# 3. install requirements
sudo -u adaptive-grid /opt/adaptive-grid/.venv/bin/pip install -r requirements.txt

# 4. konfigurasi .env (JANGAN timpa yang ada; mulai dari template)
#    sudo -u adaptive-grid cp .env.example .env   # hanya jika .env tidak ada
sudo -u adaptive-grid nano /opt/adaptive-grid/.env

# 5. install service trading
sudo cp /opt/adaptive-grid/deploy/adaptive-grid.service /etc/systemd/system/

# 6. install service dashboard
sudo cp /opt/adaptive-grid/deploy/adaptive-grid-dashboard.service /etc/systemd/system/

# 7. reload systemd
sudo systemctl daemon-reload

# 8. enable dan start kedua service
sudo systemctl enable --now adaptive-grid
sudo systemctl enable --now adaptive-grid-dashboard

# 9. verifikasi service
systemctl status adaptive-grid --no-pager
systemctl status adaptive-grid-dashboard --no-pager

# 10. verifikasi port 8080 listening
ss -ltnp | grep 8080

# 11. verifikasi dashboard API
curl -s http://127.0.0.1:8080/api/state | python3 -m json.tool | head -40

# 12. verifikasi mode PAPER (harus print paper / PAPER)
curl -s http://127.0.0.1:8080/api/state | grep -E '"execution_mode"|"session"')

# 13. verifikasi Binance TESTNET (harus print testnet)
curl -s http://127.0.0.1:8080/api/state | grep '"binance_env"'

# 14. verifikasi live gates tetap disabled (harus print false / testnet / true)
grep -E '^DRY_RUN=|^ALLOW_LIVE_EXECUTION=|^BINANCE_ENV=' /opt/adaptive-grid/.env
```

Dashboard bind `0.0.0.0:8080` via CLI flags eksplisit — unit tidak pass environment variables atau environment file ke proses dashboard. Expose publik lewat reverse proxy/tunnel sendiri jika mau; konfigurasi itu di luar repository ini.

## Testing

```bash
pytest -q
```

Suite (346+ test) deterministik dan offline: validasi konfigurasi dan live gates, matematika indikator vs referensi hand-computed, threshold masuk/keluar ketat dan prioritas keluar, quantization grid dan ekonomi executable, risk veto dan kill persistence, state restart recovery, perilaku dashboard read-only, dan integrasi bot-cycle (fill, keluar, cooldown, boundary stop, drawdown kill) melawan stub market.

## Mode Live — Peringatan

Live trading dinonaktifkan oleh setiap default. Mengaktifkannya butuh tiga gerbang eksplisit di atas dan keputusan operator sadar pada mesin di mana `.env` berisi live key. **Jangan aktifkan mode live tanpa review independen.** Penulis tidak menerima liability untuk kerugian trading.