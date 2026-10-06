# adaptive-grid 🤖📉

Bot grid **Binance Spot** konservatif multi-simbol yang vibes-nya *"abang nakal tapi sopan santunya keterbaca"*: gerbang masuk/keluar berbasis indikator ketat, validasi ekonomi grid executable, kill switch drawdown global 2%, plus dashboard read-only yang aesthetic™.

Ada dua mode eksekusi non-live: **PAPER** (simulasi internal; modal sesi dari saldo USDT testnet; literally **zero order** yang nyampe Binance — bot cuma mimpi 💭) dan **TESTNET EXECUTION** (order Spot **Testnet Binance** asli pakai dana virtual, jadi sengsaranya juga virtual 😌). Mode LIVE? Terkunci berlapis-lapis. Buka kuncinya butuh **empat gerbang sekaligus**: `EXECUTION_MODE=live`, `BINANCE_ENV=live`, `DRY_RUN=false`, `ALLOW_LIVE_EXECUTION=true`. Salah satu aja ga kebuka — fail-closed vibes only 🔒.

---

## Tujuan 🎯

Plot twist-nya sederhana: bot ini nunggu pasar lagi **santuy dan oversold**, taruh grid kecil order limit buy post-only di bawah harga, jual tiap buy yang ke-fill satu langkah grid lebih tinggi (skema cuan kecil-kecil yang halal), lalu **kabur sopan** (exit penuh) begitu kondisi pasar mulai red flag. Proteksi modal itu main character di sini, sisanya extras:

- **Trade Binance Spot doang** — tanpa futures, margin, leverage, shorting, martingale, atau averaging agresif. Bot ini ga kenal istilah "yolo all-in" dan ga mau kenal. 🚫🃏
- Masuk cuma kalau kondisi ketat terpenuhi semua; begitu kondisi memburuk, langsung keluar. Discipline-nya gigachad. 💪
- Ga pernah nempatin grid yang keuntungan net executable-nya ga bisa lewat minimum. Grid receh = ditolak. Verbal rejection. 💅
- Equity turun global 2%? Semua berhenti. Bot panik duluan daripada lo — itu namanya self-aware. 🚨

## Batasan keamanan (baca dulu, jangan skip, ini penting bestie) ⚠️

- **Ga ada jaminan profitabilitas.** Nol. Zilch. Ga ada yang di repo ini ngejanjiin cuan. Gerbang ekonomi cuma bikin bot *ga sengaja* beli grid yang udah kelihatan bakal rugi. Bedakan antara "ga rugi pasti" sama "pasti cuan" — yang pertama aja yang bisa dijamin. 🧊
- **Testnet adalah environment default.** Harga & likuiditas testnet beda dari produksi (kadang beda jauh, skibidi banget). Sinyal dari data testnet cuma buat validasi plumbing, BUKAN bukti performa. Jangan flex screenshot testnet bilang "algoritmaku menghantam". 💀
- **Equity berbasis PnL**, diankor ke `START_EQUITY`:
  `equity = START_EQUITY + realized_pnl - fees + unrealized_pnl`. Ini **bukan** rekonsiliasi saldo akun penuh ya gaes. Kill drawdown 2% ngukur kerugian terhadap basis modal simulasi.
- **Kill state ga pernah auto-reset.** Global kill atau simbol `STOPPED` oleh gerbang batas-bawah 15m itu kayak ex yang ngambek: bertahan sampai operator yang datang baikan manual. 🫠
- Gerbang batas-bawah 15m **fail-closed**: kalau data 15m-nya ghosting (ilang/invalid), bot ga nebak-nebak — dia blokir order baru buat simbol itu. Mature. Communicative. Green flag. ✅
- Rekonsiliasi fill mode live udah diimplementasi tapi belum diuji lawan kondisi produksi beneran. Jadi anggep aja mode live itu statusnya "belum prove". Jangan dipake dulu. 🙅

## Arsitektur 🏗️

```
adaptive-grid/
├── .env.example      # template konfigurasi (.env adalah SUMBER KONFIGURASI TUNGGAL, no cap)
├── config.py         # load + validasi .env -> satu objek Config immutable
├── indicators.py     # ADX/RSI/BB %B/VO/Z-score/ATR deterministik (candle CLOSED doang)
├── strategy.py       # gerbang masuk, gerbang keluar, prioritas keluar, cooldown
├── grid.py           # konstruksi grid + ekonomi executable (sadar quantization)
├── risk.py           # veto order, kill drawdown 2%, gerbang batas-bawah 15m
├── exchange.py       # Binance Spot REST (testnet default), dry-run + live executor
├── state.py          # database SQLite tunggal (state, order, fill, PnL, kills)
├── bot.py            # loop runtime yang ngorkestrasi siklus penuh
├── dashboard.py      # dashboard HTTP read-only di atas database state
└── tests/            # test suite deterministik (offline, ga borno ke internet)
```

**Ga ada YAML, ga ada lapisan kompatibilitas, ga ada duplikasi sumber konfig.** `.env` itu main character, satu-satunya. Modul lain nerima objek `Config` yang udah tervalidasi dan **ga pernah** baca environment sendiri-sendiri. No sneaky links. 🚫🔗

## Konfigurasi 🛠️

Copy `.env.example` jadi `.env`, terus isi. Kalau ada yang kurang/aneh, startup langsung **gagal dengan error yang jelas** dan nge-list SEMUA kunci bermasalah sekaligus — bukan gaen-in satu-satu kayak period tracking app. Ga ada fallback diam-diam buat nilai strategi.

Grup kunci:

| Grup | Kunci |
|---|---|
| Environment & safety | `BINANCE_ENV` (`testnet`/`live`), `EXECUTION_MODE` (`paper`/`testnet`/`live`), `DRY_RUN`, `ALLOW_LIVE_EXECUTION` |
| Scope pasar | `PAIR_LIST` (cth `BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT`), `INDICATOR_TIMEFRAME` |
| Indikator | `ADX_PERIOD`, `RSI_PERIOD`, `BB_PERIOD`, `BB_STD`, `VO_FAST`, `VO_SLOW`, `ZSCORE_PERIOD`, `ATR_PERIOD` |
| Gerbang masuk (SEMUA harus lolos) | `ENTRY_ADX_MAX`, `ENTRY_RSI_MAX`, `ENTRY_VOLUME_OSC_MIN`, `ENTRY_BB_PERCENT_B_MAX` |
| Gerbang keluar (SALAH SATU = gas keluar) | `EXIT_RSI_MIN`, `EXIT_ADX_MIN`, `EXIT_BB_PERCENT_B_MIN`, `EXIT_ZSCORE_ABS_MAX` |
| Ekonomi grid | `ATR_GRID_MULTIPLIER`, `GRID_GROSS_MIN`, `MIN_NET_PROFIT_PER_GRID`, `MAKER_FEE`, `TAKER_FEE`, `SLIPPAGE_ESTIMATE` |
| Risiko | `MAX_DRAWDOWN_PERCENT` (hard cap 2), `STOP_IF_BELOW_LOWER_PERCENT` (hard cap 2), `START_EQUITY`, `COOLDOWN_HOURS` |
| Kredensial | `BINANCE_TESTNET_API_KEY/SECRET` (testnet), `BINANCE_API_KEY/SECRET` (produksi; ga kepake kecuali live benar-benar kebuka gerbangnya) |

Hard floor yang divalidasi (coba di-nerf, bakal ditolak config — bot ini literally ga bisa di-bullied): `GRID_GROSS_MIN >= 0.005` (0.50%), `MIN_NET_PROFIT_PER_GRID >= 0.002` (0.20%), `MAX_DRAWDOWN_PERCENT <= 2`, `STOP_IF_BELOW_LOWER_PERCENT <= 2`.

> Catatan: nama lama `GRID_STEP_ATR_MULTIPLIER` masih diterima (legacy friendly, biar `.env` deployment lama ga break), tapi `ATR_GRID_MULTIPLIER` nama resminya sekarang. Nama baru, vibes tetap sama.

### Mode eksekusi 🎮

| `BINANCE_ENV` | `EXECUTION_MODE` | Perilaku |
|---|---|---|
| `testnet` | `paper` (default) | Simulasi internal; modal sesi dari saldo USDT testnet pas sesi dibuat; **order Binance ga pernah dikirim — periodt**; transaksi paper ga pernah nyentuh akun exchange. |
| `testnet` | `testnet` | Order **Spot Testnet Binance** asli pakai kredensial testnet doang; wajib `DRY_RUN=false`; exchange jadi sumber kebenaran buat fill, fee, dan inventory. |
| `live` | `live` | Eksekusi produksi — **terkunci** kecuali `DRY_RUN=false` **dan** `ALLOW_LIVE_EXECUTION=true` **dan** live key ada. Tiga kunci, satu pintu. 🔐 |

Konsistensi dipaksa saat startup (fail closed): `EXECUTION_MODE=live` wajib `BINANCE_ENV=live`, dan sebaliknya; `EXECUTION_MODE=testnet` wajib `DRY_RUN=false` + kredensial testnet; mode testnet **ga pernah** nyentuh endpoint produksi (base URL dipilih gerbang konfigurasi, bukan difantasisasi dari key). Bot ga bakal "ke-peleset" ke mainnet. Ga ada slip itu di kamusnya.

### Modal sesi (paper/testnet) 💰

Pas start pertama, bot bikin **sesi eksekusi**: modalnya dari `START_EQUITY` (kalau > 0) atau dari saldo USDT testnet (butuh kredensial testnet; fail-closed kalau ga ada / nol — dia ga bakal ngarang modal). Equity = `modal sesi + realized PnL - fees + unrealized PnL`; saldo wallet cuma telemetry buat dipandang-dipandang, ga pernah diimpor ulang ke ledger paper. Sesi persist: restart = lanjut modal yang sama, session id sama, baseline drawdown sama. Ganti `EXECUTION_MODE`/`BINANCE_ENV` lawan database sesi yang udah ada? Startup ditolak. Escape hatch-nya satu dan deterministik:

```bash
python bot.py --reset-session    # nolak kalau masih ada order terbuka; hapus sesi + kill state
```

### Gerbang live-trading 🔐

Endpoint & kredensial live dipake **cuma kalau** **ketiganya** terpenuhi:

1. `DRY_RUN=false`
2. `ALLOW_LIVE_EXECUTION=true`
3. `BINANCE_ENV=live`

Kombinasi laen? Tetep di testnet, atau ditolak startup (cth: env `live` dengan `DRY_RUN=false` tapi gerbang lain masih ketutup = ditolak). `DRY_RUN=true` (default) = ga pernah nembak order ke Binance. Bot cuma jalan-jalan liat-liat pasar, window shopping aja. 🛍️

## Aturan Strategi 🧠

**Masuk** (simbol boleh mulai grid cuma kalau SEMUA lolos, diukur di candle CLOSED `INDICATOR_TIMEFRAME`):

- ADX(14) < 20 — pasar harus lagi *santuy*, lagi ga ada drama trend
- RSI(14) <= 40 — lagi diskon, minimal diskon tipis-tipis
- Volume Oscillator(5,10) >= 0 — ada yang nyari, minimal nyari-nyari dikit
- Bollinger %B(20,2) <= 0.20 — harga lagi mampir di lantai bawah band

Riwayat candle kurang? **NO TRADE.** Bukan "ah yaudah kira-kira lah", bukan exception kambuh, bukan nilai karangan. Zero trade. Ini yang bikin ortu nyaman. 🧘

**Keluar** (grid aktif keluar kalau SALAH SATU kena; keluar SELALU prioritas di atas masuk):

- RSI(14) >= 70
- ADX(14) > 25
- Bollinger %B > 1
- abs(Z-Score(20)) > 2.5

Keluar otomatis itu SOP yang rapi: stop order baru → cancel semua order terbuka → **verifikasi** → market-sell inventory yang dipegang → **verifikasi lagi** → catat alasan keluar, realized PnL, fee → cooldown (`COOLDOWN_HOURS`, default 3 jam) → ga ada re-entry otomatis selama cooldown. Verifikasi gagal dikit aja? Fail-closed: simbol berhenti di `ERROR` buat ditengok operator. Bot ga pernah "yaudah anggep aja berhasil". Never delulu. 🙃

Setelah keluar otomatis, simbol ga bisa mulai grid baru sebelum cooldown selesai — dan cooldown tetap bertahan walau botnya di-restart. Nggak bisa di-tgep. 💅

Perintah `--check-grid` (read-only) ngevalidasi grid + ekonomi net executable-nya buat semua simbol sebelum eksekusi beneran:

```bash
python bot.py --check-grid
```

Ini pake **jalur produksi persis** (`MarketData` + `grid.build_grid`): ambil filter exchange, harga rata-rata terbobot reference, ATR candle closed, bangun plan dengan logika quantization/fee/slippage/PERCENT_PRICE_BY_SIDE yang sama kayak live, terus cetak ACCEPTED/REJECTED per simbol lengkap dengan min/max/average executable net profit dan level yang di bawah minimum. Ga submit apa-apa, ga cancel apa-apa, ga sentuh database state. Exit non-zero kalau ada simbol yang gagal gerbang. Jujur itu mahal, tapi di sini gratis. ✨

## Ekonomi Grid 📐

- Grid step = yang lebih besar antara `ATR_GRID_MULTIPLIER × ATR(14)` dan **economic minimum step**. Jadi pas volatilitas ngilang (ATR mungil), grid ga tumbang cuma karena langkahnya kegedean buat nutup fee — step-nya di-*floor* ke ukuran minimum yang masih economically viable, dihitung dari fee + slippage + required profitability (deterministik, bukan angka sulap). Setelah itu grid **direbuild ulang** dan divalidasi penuh. Kandidat ga lolos = ditolak. Ekonomi ga pernah diturunin demi "biar kebuka". 💢
- Grid arithmetic buat BTC/ETH/BNB, geometric buat SOL (mapping ada di `config.py`).
- Harga buy dibulatkan **ke bawah**, sell **ke atas** ke tick size exchange; qty nge-hormatin step size dan minimum notional. Konservatif itu default aesthetic-nya.
- **Ekonomi executable** (setelah quantization exchange, plus fee buy, fee sell, dan estimasi slippage dua sisi) itu yang jadi hakim. Bukan angka teoretis yang cakep di spreadsheet. 📊
- Grid **diblokir** kecuali executable gross >= 0.50% DAN executable net >= 0.20% (`GRID_GROSS_MIN`, `MIN_NET_PROFIT_PER_GRID`). Grid jelek ga pernah diperlebar, dipaksa, atau dibujuk. Rejection tanpa drama.
- **PERCENT_PRICE_BY_SIDE di-parse dan di-enforce lokal.** Buat tiap simbol, harga rata-rata terbobot exchange (`GET /api/v3/avgPrice`, via filter `avgPriceMins` — ga dianggap sama dengan last price, karena bot ga suka asumsi) jadi reference: level BUY harus ada di `reference × [bidMultiplierDown, bidMultiplierUp]`, harga SELL di `reference × [askMultiplierDown, askMultiplierUp]`. Level yang bandel di luar band di-drop dari plan (atau grid diblokir kalau ga ada sisa); child sell di luar band di-defer ke siklus berikutnya, bukan dibanting ke Binance biar ditolak. Penolakan definitif (HTTP 400 filter failure, kode -1013/-2010) bikin simbol berhenti di `ERROR` — ga pernah di-retry buta. Ghosted by the exchange? Bot langsung stop, ga nagih-nagih. 🚫👻

## Aturan Risiko 🚨

- **Global drawdown kill switch: 2%** (`MAX_DRAWDOWN_PERCENT`, hard cap). Drawdown diukur lawan high-water mark `equity = START_EQUITY + realized - fees + unrealized`. Kalau kena: kill **di-latch dan persist duluan** (ga ada order baru global), terus per simbol semua order dicancel & diverifikasi, semua inventory dilikuidasi & diverifikasi. Cancel/likuidasi gagal verifikasi = fail-closed (simbol `ERROR` + risk event) tapi **kill ga pernah kehapus** — tetep nyala lintas restart sampai operator yang turun tangan. Bot-nya pegang, bot-nya yang bertanggung jawab. 🫡
- **Proteksi batas-bawah 15m** (`STOP_IF_BELOW_LOWER_PERCENT`, hard cap 2%): kalau **CLOSE candle 15m terakhir** (bukan wick intrabar yang cuma bohong) tutup paling banyak `lower × (1 - 2%)`, simbol dihentikan (order dicancel, inventory dilikuidasi, state `STOPPED`). Data 15m ilang/invalid = blokir order baru buat simbol itu (fail-closed), bukan mikir-mikir "mungkin sih aman". Ga mungkin-mungkin. 🙅
- Risk engine punya **wewenang veto atas SETIAP order**: global kill, simbol risk-stopped, atau simbol error = semua placement diveto. Risk engine itu HR-nya bot ini. Ga ada yang lewat. 🧑‍💼
- Order pake LIMIT_MAKER (post-only) kalau didukung, bawa client order id unik, dipersist **sebelum** submit, dan **ga pernah di-retry buta**: gagal network → direkonsiliasi by client id dulu; state final unknown → fail-closed, simbol dihentikan. Kalau ga yakin, jangan ngaku-ngaku berhasil. 🧾
- **Akuntansi fill idempotent per trade exchange**: trade id = kunci idempotensi; satu transaksi atomis nyatet fill + update inventory, average cost, realized PnL. Partial fill dihitung pas terjadi pakai qty executed aktual — bukan qty planned yang on paper doang. Rekonsiliasi berkali-kali atau restart seribu kali ga akan double-count. Ledger-nya bucin sama akurasi. 📓💚
- BUY yang ke-fill dikonversi jadi child SELL order pakai **qty aktual yang diterima** (net komisi base asset; dilacak per order di `child_sell_qty`, diupdate atomis bareng pembuatan child) — duplicate child sell secara struktural mustahil. Kayak OTP yang udah dipake. ♻️
- Likuidasi pake satu client order id per attempt (dilacak end-to-end), rekonsiliasi id eksak setelah error network, sisa dihitung dari qty executed aktual, terus hasilnya diverifikasi lawan **saldo base asset di akun** (otoritatif, testnet/live). Verifikasi gagal atau masih ada sisa = TIDAK dilikuidasi, simbol fail-closed. Ga ada setengah-setengah. 🎯

## State 🗃️

Satu database SQLite (`state.db` default, path via `--db`) nampung semua: state global bot, per-simbol state, cooldown, nilai plan grid, order, fill, fee, realized PnL, risk event, kill state, plus **telemetry entry blocker** (statistik read-only: berapa kali dicblock ADX/RSI/VO/BB, grid economics, budget, risk veto, cooldown, jumlah entry sukses, entry blocker terakhir, alasan grid rejection terakhir). Schema bawa version stamp (`schema_version` di `meta`), dimigrasi deterministik minimal saat startup. Ga ada tabel kompatibilitas buat arsitektur lama — move on, heal, glow up. ✨

## Dashboard 📡

```bash
python dashboard.py --db state.db --host 127.0.0.1 --port 8080
```

Konsol operator retrofuturistik read-only (*"sistem kontrol trading crypto serius dari 1987 alternatif"*): display CRT near-black dengan grid teknis halus + scanlines, warna aksen fosfor, tipografi monospace, panel KPI bergaya instrumen, modul telemetry per-simbol buat tiap pair terkonfigurasi, dan chart net-PnL bergaya oscilloscope. Auto-refresh tiap 5 detik tanpa reload; kalau feed gagal, tampil `DATA STALE` dan nilai baik terakhir dipertahankan — **ga ada yang difabrikasi**, `DATA LIVE` balik sendiri begitu API sehat. Environment (`TESTNET`/`LIVE`) dan execution mode di header itu record runtime sendiri — ga bisa di-set/ubah dari dashboard. Read-only beneran, bukan read-only *katanya*.

Routes (GET only): `GET /` (shell console — static HTML/CSS/JS, no external assets), `GET /api/state` (JSON snapshot, termasuk `entry_telemetry` per simbol), `GET /api/history` (telemetry net PnL kumulatif dari fill ledger; kosong kalau emang ga ada fill — history ga pernah dikarang). Semua method write = `405`. Console ga punya logic strategi, ga place/cancel apa pun, ga expose kredensial, ga pernah baca `.env` atau environment variable, dan render nilai dynamic murni lewat DOM API aman (`textContent`). Skema keamanannya tertib. 🧷

Data yang tampil: equity global, reference equity, drawdown, open orders, realized PnL, fees, kill switch state/reason, runtime & database status; per simbol: state, risk status, price (harga ticker live), timeframe, entry status/blocker, exit status/reason, cooldown, grid mode/step/count, gross & net per grid, inventory, average cost, open orders, realized PnL, fees, dan entry telemetry.

State simbol ditampilkan **verbatim** dari database — ga pernah di-infer: `WAITING`, `ENTRY_BLOCKED`, `GRID_BLOCKED`, `ACTIVE`, `COOLDOWN`, `EXITING`, `STOPPED`, `KILL_ACTIVE`, `ERROR`. Database-nya down? Console tetep hidup dan nampilin `DATABASE UNAVAILABLE`, bukan crash. Stabil di masa sulit. 💪

## Instalasi 🧰

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # terus edit .env, jangan males
```

Python 3.10+ recommended. Dependency runtime minimal: `python-dotenv` (plus `pytest` buat test suite). Ringan, ga drama, ga node_modules sepanjang sungai. 🪶

## Menjalankan (dry-run) 🏃

```bash
python bot.py --once          # satu siklus, cobain dulu vibes-nya
python bot.py                 # loop (siklus 30s); mode PAPER default
```

Startup log nampilin environment sebagai `TESTNET` atau `LIVE` (bukan kredensial, tenang) dan nolak start kalau ada masalah konfigurasi apa pun.

### Setup Testnet 🧪

1. Bikin API key di <https://testnet.binance.vision/> — **izin Spot trading aja; withdrawal mati.** Selalu mati. Non-negotiable.
2. Masukin ke `.env` di `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`. Ga pernah di-commit, ga pernah di-share, ga pernah di-screenshot buat story. 🤐
3. Mode PAPER (default) pake key ini cuma buat baca saldo (modal sesi); ga ada yang di-submit.
4. Buat eksekusi order testnet beneran, set `EXECUTION_MODE=testnet` dan `DRY_RUN=false`. Validasi dulu step-by-step, jangan gas full send:

```bash
python bot.py --check-exchange                       # konektivitas/auth/filter (read-only)
python bot.py --check-grid                           # konstruksi grid + ekonomi net executable (read-only, no order)
python bot.py --testnet-order-selftest BTC/USDT      # place + verifikasi + cancel satu order jauh dari market
python bot.py                                        # jalankan eksekusi testnet
```

Order self-test itu flag-gated dan ga pernah jalan otomatis; dia nempatin LIMIT_MAKER buy 50% di bawah market terus cancel — secuek apa pun market-nya, order itu ga mungkin ke-fill. It's giving *safety drill*. 🧯

## Deployment (VPS / systemd) 🐧

Template deploy ada di `deploy/`. Kedua service jalan sebagai user `adaptive-grid` di `/opt/adaptive-grid` dan share **satu database otoritatif**: `/opt/adaptive-grid/state.db`. Runtime trading yang pegang semua write; dashboard buka read-only. Ga ada database kedua — unit lama yang nunjuk DB legacy di `data/` udah gado-gado dan harus diganti.

Fresh deploy (VPS existing: simpan `state.db` dan `.env`):

```bash
# 1. clone atau update repository
sudo -u adaptive-grid git -C /opt/adaptive-grid pull --ff-only origin main
# (mesin baru: sudo git clone https://github.com/rmdnl/adaptive-grid.git /opt/adaptive-grid)

# 2. buat/refresh virtualenv
cd /opt/adaptive-grid && sudo -u adaptive-grid python3 -m venv .venv

# 3. install requirements
sudo -u adaptive-grid /opt/adaptive-grid/.venv/bin/pip install -r requirements.txt

# 4. konfigurasi .env (JANGAN timpa yang ada; mulai dari template)
#    sudo -u adaptive-grid cp .env.example .env   # cuma kalau .env belum ada
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
curl -s http://127.0.0.1:8080/api/state | grep -E '"execution_mode"|"session"'

# 13. verifikasi Binance TESTNET (harus print testnet)
curl -s http://127.0.0.1:8080/api/state | grep '"binance_env"'

# 14. verifikasi live gates tetap disabled (harus print false / testnet / true)
grep -E '^DRY_RUN=|^ALLOW_LIVE_EXECUTION=|^BINANCE_ENV=' /opt/adaptive-grid/.env
```

Dashboard bind `0.0.0.0:8080` via CLI flags eksplisit — unit ga ngoper environment variables atau environment file ke proses dashboard. Mau expose publik? Lewat reverse proxy/tunnel sendiri; konfigurasi itu di luar repo ini. (Dan ya, dashboard tanpa auth itu gaya hidup berisiko — kasih proxy auth kalau ga mau saldo lo jadi konten publik. 💀)

## Testing 🧪

```bash
pytest -q
```

Suite (**391 test**) deterministik dan offline: validasi konfigurasi & live gates, matematika indikator lawan referensi hand-computed, threshold masuk/keluar ketat + prioritas keluar, quantization grid & ekonomi executable, risk veto & kill persistence, state restart recovery, retry jujur (GET boleh di-retry, order ga pernah di-retry buta), perilaku dashboard read-only, telemetry entry blocker, dan integrasi bot-cycle (fill, keluar, cooldown, boundary stop, drawdown kill) lawan stub market. Test-nya lebih banyak dari followers pertama lo. 🔥

## Mode Live — Peringatan 🚨

Live trading itu **disabled by default** dan itu bukan accident, itu desain. Buat ngaktifin butuh tiga gerbang eksplisit di atas PLUS keputusan operator yang sadar dan penuh kesadaran di mesin yang `.env`-nya beneran berisi live key. **Jangan aktifin mode live tanpa review independen.** Nggak ada dukun, nggak ada sinyal grup, nggak ada "katanya". Penulis ga nerima liability buat kerugian trading — kerugian lo ya lo yang pegang, bestie. Kalau ragu: tetep di paper/testnet, santuy, ga usah buru-buru. Market bakal masih buka besok. 🧘‍♂️
