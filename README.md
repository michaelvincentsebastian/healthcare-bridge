# Dokumentasi analytics-bridge

DuckDB Quack Server — Bridge Read-Only ke MariaDB (Frappe/SatuSehat), Pengganti REST API Bridging

---

## 1. Ringkasan Umum

`analytics-bridge` adalah service kecil yang jalan 24/7 di dalam container, tugasnya satu: membuka "pintu baca" ke database MariaDB (sumbernya Frappe framework, dengan modul healthcare/SatuSehat) supaya bisa diquery langsung pakai SQL oleh tool analitik seperti SQLMesh, DBeaver, atau script Python — **tanpa** harus lewat REST API custom.

Inti dari service ini adalah proses Python (`serve.py`) yang:

1. Membuka satu koneksi DuckDB in-memory.
2. Meng-`ATTACH` MariaDB sebagai sumber data eksternal lewat `mysql` extension (mode `READ_ONLY`).
3. Membuatkan **view** DuckDB untuk tiap tabel yang di-whitelist, dikumpulkan dalam schema `bridge`.
4. Menjalankan `quack` extension untuk mem-broadcast koneksi DuckDB itu sebagai server jaringan di port `9494`, dilindungi token dan guard read-only.
5. Idle selamanya (heartbeat tiap 5 menit) sambil menunggu request masuk, sampai container di-stop.

Jadi alurnya: **client → quack server → view DuckDB → mysql extension → MariaDB**, dan hasilnya balik lagi ke client dalam bentuk resultset DuckDB biasa.

---

## 2. Kenapa Pendekatan Ini, Dibanding REST API Bridging

| Aspek | REST API bridging (cara lama) | analytics-bridge (DuckDB quack server) |
|---|---|---|
| Cara akses data | Bikin endpoint per kebutuhan, response JSON | Client kirim SQL langsung, dapat resultset tabular |
| Kebutuhan join/agregasi lintas tabel | Harus dikerjakan di kode backend (N+1 query, atau bikin endpoint khusus tiap kombinasi) | Cukup ditulis sebagai SQL — join, filter, agregasi bebas selama read-only |
| Cocok untuk tool analitik (SQLMesh dkk) | Butuh adapter/connector custom untuk parsing JSON | SQLMesh & DuckDB client bisa connect langsung, karena protokolnya native DuckDB |
| Overhead development | Nambah endpoint tiap ada kebutuhan data baru | Cukup tambah nama tabel ke `WHITELISTED_TABLES`, tidak perlu tulis kode baru |
| Serialisasi data | JSON (lossy untuk beberapa tipe, perlu skema response) | Tabular native — tipe data dari MariaDB terjaga |
| Keamanan | Auth per endpoint | Token tunggal + guard regex yang cuma izinkan `SELECT/FROM/WITH/EXPLAIN/DESCRIBE/SHOW` |

Trade-off yang perlu disadari: karena aksesnya SQL langsung (bukan endpoint terkurasi), **whitelist tabel dan guard read-only jadi satu-satunya lapisan kontrol** atas apa yang boleh dibaca. Tidak ada logika bisnis atau validasi per-field seperti di REST API — jadi cocoknya untuk konsumen internal/tepercaya (tim data, SQLMesh), bukan untuk expose ke publik.

---

## 3. Arsitektur & Komponen File

```
analytics-bridge/
├── Dockerfile           # image python:3.12-slim + duckdb
├── docker-compose.yaml  # definisi service, network, healthcheck
├── serve.py             # proses utama: attach MariaDB, bikin view, jalankan quack server
├── healthcheck.py       # dipanggil Docker HEALTHCHECK, tes jalur penuh end-to-end
└── requirements.txt     # duckdb==1.5.4, python-dotenv
```

Container ini **tidak menyimpan data**. Semua tabel di schema `bridge` adalah *view*, bukan copy — tiap query yang masuk akan diteruskan (scan-through) ke MariaDB secara real-time lewat `mysql` extension DuckDB. Karena itu bridge ini stateless dan aman untuk di-restart kapan saja tanpa risiko kehilangan data.

---

## 4. Mekanisme Kerja — Detail per Tahap

### 4.1 Startup: `build_connection()`

1. Buka koneksi DuckDB baru (`duckdb.connect()`), lalu install & load dua extension: `mysql` dan `quack`.
2. `ATTACH` ke MariaDB pakai environment variable `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_DB`, dengan alias `frappe_src` dan flag `READ_ONLY`.
3. **Sanity check fail-fast**: karena `ATTACH` di DuckDB itu *lazy* (tidak benar-benar connect sampai ada query jalan), kode menjalankan `SELECT 1 FROM frappe_src.information_schema.tables LIMIT 1` supaya kalau kredensial/host salah, error langsung muncul saat startup — bukan nanti pas SQLMesh baru mencoba query.
4. Bikin schema `bridge` kalau belum ada.
5. Loop semua tabel di `WHITELISTED_TABLES`, untuk tiap tabel:
   - Nama view dibuat dari nama tabel: lowercase, spasi jadi underscore (`tabPatient Encounter` → `bridge.tabpatient_encounter`).
   - `CREATE OR REPLACE VIEW bridge."<view>" AS SELECT * FROM frappe_src."<tabel>"`.
   - Divalidasi dengan `SELECT * ... LIMIT 1` (bukan `COUNT(*)` — lihat catatan bug di bagian 9).
   - Hasil sukses/gagal dicatat ke log, dan di akhir loop ada ringkasan "X/Y view siap".

### 4.2 Serving: `main()`

1. Panggil `CALL quack_serve('quack:0.0.0.0:9494', allow_other_hostname => true, token => '<QUACK_TOKEN>')` — ini yang membuka listener jaringan di port 9494 pakai token dari environment variable.
2. Buat macro SQL `read_only(sid, query)` yang mengembalikan `true` hanya kalau query (setelah di-trim & uppercase) diawali `SELECT`, `FROM`, `WITH`, `EXPLAIN`, `DESCRIBE`, atau `SHOW`.
3. `SET GLOBAL quack_authorization_function = 'read_only'` — daftarkan macro itu sebagai gatekeeper: setiap query yang masuk lewat quack server dicek dulu, dan ditolak kalau bukan operasi baca.
4. Masuk ke wait-loop yang menangani `SIGTERM`/`SIGINT` supaya `docker compose down` atau restart bisa graceful shutdown (bukan `input()`, karena container `-d` tidak punya stdin — akan langsung `EOFError`).
5. Tiap 5 menit sekali menulis log heartbeat, supaya kalau proses hang tanpa crash, itu ketahuan dari log yang berhenti muncul.
6. Saat sinyal stop diterima: `CALL quack_stop(...)` untuk mematikan listener dengan bersih.

### 4.3 Health check: `healthcheck.py`

Dipanggil Docker tiap 15 detik (`interval: 15s`, `timeout: 10s`, `retries: 3`, `start_period: 20s`). Bukan sekadar cek proses hidup — script ini benar-benar **connect sebagai client** ke quack server (`quack_query` ke `quack:localhost:9494`) dan query salah satu view (default `bridge.tabpatient`, bisa dioverride lewat env `HEALTHCHECK_VIEW`). Kalau berhasil → exit 0 (sehat). Kalau gagal (server down, token salah, koneksi ke MariaDB putus, dst) → exit 1 (unhealthy).

Dengan begitu, healthcheck ini menguji **jalur penuh**: quack server → sesi DuckDB → mysql extension → MariaDB — bukan cuma "proses python masih jalan".

---

## 5. Keamanan

Ada tiga lapis kontrol:

1. **Token authentication** — `QUACK_TOKEN` wajib diisi lewat environment variable (bukan digenerate random tiap start). Tanpa token yang cocok, client tidak bisa connect ke server di port 9494.
2. **Read-only guard (`quack_authorization_function`)** — setiap query yang masuk dicek regex-nya harus diawali salah satu dari `SELECT | FROM | WITH | EXPLAIN | DESCRIBE | SHOW`. Query `INSERT`, `UPDATE`, `DELETE`, `DROP`, dsb otomatis ditolak di level ini.
3. **Read-only di sumber data** — koneksi `ATTACH` ke MariaDB juga pakai flag `READ_ONLY`, jadi meskipun guard di atas somehow terlewati, koneksi ke MariaDB-nya sendiri sudah tidak bisa menulis.
4. **Whitelist tabel eksplisit** — hanya tabel yang ada di `WHITELISTED_TABLES` yang punya view. Tabel lain di MariaDB (termasuk tabel sensitif seperti user/auth Frappe) sama sekali tidak terekspos lewat schema `bridge`.

Catatan operasional: karena semua kontrol ini ada di level query/koneksi (bukan per-field), **siapa pun yang punya token bisa membaca seluruh isi tabel yang di-whitelist**. Bagikan token hanya ke konsumen tepercaya (SQLMesh, tim data), dan simpan lewat secret manager / `.env` yang tidak ikut ke-commit ke git.

---

## 6. Konfigurasi (Environment Variables)

Bridge ini butuh file `.env` di folder yang sama dengan `docker-compose.yaml` (di-reference lewat `env_file: - .env`). Berikut variabel yang dipakai:

| Variabel | Wajib? | Dipakai di | Keterangan |
|---|---|---|---|
| `DB_HOST` | Ya | `serve.py` | Host MariaDB sumber (biasanya alias container di `db_network`) |
| `DB_PORT` | Ya | `serve.py` | Port MariaDB, biasanya `3306` |
| `DB_USER` | Ya | `serve.py` | User MariaDB — sebaiknya user dengan privilege read-only saja |
| `DB_PASSWORD` | Ya | `serve.py` | Password user di atas |
| `DB_DB` | Ya | `serve.py` | Nama database Frappe yang mau di-bridge |
| `QUACK_TOKEN` | Ya | `serve.py`, `healthcheck.py` | Token auth untuk client quack. Harus tetap/statis, jangan random tiap restart |
| `SERVER_HOST` | Tidak (default `127.0.0.1`) | `docker-compose.yaml` | Interface host tempat port di-bind. `127.0.0.1` = hanya bisa diakses dari mesin yang sama |
| `SERVER_PORT` | Tidak (default `9494`) | `docker-compose.yaml` | Port yang di-expose ke host |
| `MARIADB_CONNECTION_NETWORK` | Tidak (default `mariadb-network`) | `docker-compose.yaml` | Nama Docker network eksternal tempat MariaDB berada |
| `HEALTHCHECK_VIEW` | Tidak (default `bridge.tabpatient`) | `healthcheck.py` | View yang dipakai untuk probe healthcheck |

Contoh `.env`:

```env
# Koneksi ke MariaDB sumber (Frappe)
DB_HOST=mariadb
DB_PORT=3306
DB_USER=bridge_readonly
DB_PASSWORD=ganti_dengan_password_kuat
DB_DB=nama_database_frappe

# Token statis untuk autentikasi quack server
QUACK_TOKEN=ganti_dengan_token_panjang_dan_acak

# (Opsional) Binding & network
SERVER_HOST=127.0.0.1
SERVER_PORT=9494
MARIADB_CONNECTION_NETWORK=mariadb-network

# (Opsional) View probe untuk healthcheck
HEALTHCHECK_VIEW=bridge.tabpatient
```

> Rekomendasi: buat user MariaDB khusus dengan privilege `SELECT` saja untuk `DB_USER`, jangan pakai user admin. Ini jadi lapisan pertahanan tambahan di luar guard read-only yang sudah ada di kode.

---

## 7. Instalasi & Deployment

### Prasyarat

- Docker & Docker Compose terpasang.
- Network Docker eksternal untuk MariaDB sudah ada (default nama `mariadb-network`), dan MariaDB sudah reachable di network itu.
- MariaDB memiliki user dengan akses baca ke database Frappe yang dituju.

### Langkah

```bash
# 1. Masuk ke folder project
cd analytics-bridge

# 2. Buat file .env (isi sesuai tabel konfigurasi di atas)
cp .env.example .env   # atau buat manual
nano .env

# 3. Pastikan network eksternal MariaDB sudah ada
docker network ls | grep mariadb-network
# kalau belum ada:
docker network create mariadb-network

# 4. Build & jalankan
docker compose up -d --build

# 5. Cek log startup — pastikan semua view "siap"
docker compose logs -f duckdb-bridge
```

Yang harus muncul di log kalau sukses:

```
Menyambung ke MariaDB <host>:<port>/<db> ...
Koneksi ke MariaDB berhasil.
view bridge.tabpatient siap (ada data)
...
Ringkasan: 39/39 view siap.
Quack server listening di quack:0.0.0.0:9494 (read-only enforced)
```

Kalau ada tabel yang gagal di-mapping, akan muncul warning `Tabel gagal di-mapping: [...]` — service tetap jalan untuk tabel yang berhasil, tapi tabel yang gagal itu perlu dicek manual (lihat bagian Troubleshooting).

### Update / restart

```bash
docker compose down
docker compose up -d --build
```

Karena semua state ada di MariaDB (bridge-nya stateless), restart kapan saja aman — tidak ada data yang hilang, view akan dibuat ulang otomatis saat startup.

---

## 8. Cara Penggunaan (Sisi Client)

### 8.1 Dari SQLMesh

Bridge ini didesain supaya SQLMesh bisa treat-nya seperti sumber data DuckDB biasa, dengan `bridge.*` sebagai schema sumber. Contoh koneksi konseptual (sesuaikan dengan adapter/connection string yang dipakai SQLMesh untuk DuckDB attach/quack):

```python
import duckdb

con = duckdb.connect()
con.execute("INSTALL quack; LOAD quack;")

# Attach ke quack server sebagai remote database
con.execute("""
    ATTACH 'quack:<host_bridge>:9494' AS bridge_remote
    (TYPE quack, token '<QUACK_TOKEN>')
""")

df = con.execute("SELECT * FROM bridge_remote.bridge.tabpatient LIMIT 10").df()
```

> Sintaks pasti `ATTACH` untuk tipe `quack` sebaiknya dicek ulang terhadap versi extension yang dipakai — perilaku detail di atas mengikuti pola `quack_query` yang dipakai di `healthcheck.py`, bukan dari dokumentasi resmi extension `quack` yang terverifikasi di sini.

### 8.2 Query langsung ala healthcheck (`quack_query`)

Cara paling sederhana untuk uji-coba manual, mengikuti pola yang sama dengan `healthcheck.py`:

```python
import duckdb

con = duckdb.connect()
con.execute("INSTALL quack; LOAD quack;")

result = con.execute("""
    FROM quack_query(
        'quack:<host_bridge>:9494',
        'SELECT * FROM bridge.tabpatient LIMIT 5',
        token = '<QUACK_TOKEN>',
        disable_ssl => true
    )
""").fetchall()

print(result)
```

Ganti `<host_bridge>` dengan hostname container (`duckdb-quack-bridge` kalau dari network Docker yang sama) atau `127.0.0.1` kalau dari mesin host (sesuai `SERVER_HOST`/`SERVER_PORT` di `.env`).

### 8.3 Aturan query yang berlaku

- Hanya statement yang diawali `SELECT`, `FROM`, `WITH`, `EXPLAIN`, `DESCRIBE`, atau `SHOW` yang akan diterima — selain itu ditolak oleh guard `read_only`.
- Nama view mengikuti pola `bridge.<nama_tabel_lowercase_underscore>`, contoh: `tabPatient Encounter` → `bridge.tabpatient_encounter`.
- Hindari `COUNT(*)` langsung di atas view (lihat catatan bug di bagian 9) — kalau butuh jumlah baris, pertimbangkan `SELECT count(*) FROM (SELECT id FROM bridge.<view>) t` atau query serupa yang menghindari pushdown bermasalah, dan uji dulu di lingkungan non-produksi.

---

## 9. Catatan Teknis Penting (Gotcha dari Kode)

Beberapa keputusan desain di kode ini sengaja didokumentasikan lewat komentar karena berasal dari bug/insiden yang pernah ditemui. Penting untuk dipahami supaya tidak "diperbaiki balik" secara tidak sengaja:

1. **Nama tabel di whitelist sudah termasuk prefix `tab`.** Konvensi Frappe menyimpan tabel dengan prefix `tab` (misal `tabPatient`). Jangan tambahkan prefix ulang di loop pembuatan view — pernah jadi bug `tabtabPatient` yang bikin `CREATE VIEW` gagal.
2. **`ATTACH` di DuckDB itu lazy.** Tidak ada validasi koneksi sampai query pertama dijalankan. Karena itu ada query sanity-check kecil tepat setelah `ATTACH`, supaya kalau host/kredensial salah, error muncul saat startup (fail-fast) — bukan nanti saat SQLMesh baru mulai jalan.
3. **Jangan pakai `COUNT(*)` di atas view yang men-scan tabel via `mysql` extension.** Ini memicu bug internal DuckDB (`count_star` pushdown salah resolve column binding → `INTERNAL Error`/assertion failure). Validasi view di kode ini sengaja pakai `SELECT * ... LIMIT 1`, bukan `COUNT(*)`.
4. **Jangan pakai `input()` untuk keep-alive proses di container.** Container yang jalan dengan `-d` tidak punya stdin terbuka — `input()` langsung `EOFError` dan proses exit. Solusinya pakai wait-loop yang merespons `SIGTERM`/`SIGINT`, supaya `docker compose down`/restart bisa graceful.
5. **`QUACK_TOKEN` harus statis dari environment, bukan digenerate ulang tiap start.** Kalau random tiap restart, healthcheck maupun client SQLMesh tidak akan pernah tahu token aktif tanpa baca log container secara manual — ini akan mematahkan otomasi.

---

## 10. Troubleshooting

| Gejala | Kemungkinan Penyebab | Yang Perlu Dicek |
|---|---|---|
| Container langsung exit setelah start | `ATTACH` gagal (host/kredensial MariaDB salah), atau env var wajib belum diisi | `docker compose logs duckdb-bridge`, cek pesan error di sekitar "Menyambung ke MariaDB" |
| Sebagian tabel muncul di "Tabel gagal di-mapping" | Nama tabel di whitelist tidak cocok persis dengan nama tabel asli di MariaDB, atau tabel memang belum ada / permission ditolak | Cocokkan nama tabel via `SHOW TABLES` langsung ke MariaDB, cek privilege user `DB_USER` |
| Healthcheck `unhealthy` terus | Quack server belum siap (masih dalam `start_period`), token salah, atau koneksi ke MariaDB putus di tengah jalan | `docker compose logs`, cek heartbeat terakhir, coba jalankan `healthcheck.py` manual di dalam container |
| Client tidak bisa connect dari luar container | `SERVER_HOST` di-bind ke `127.0.0.1` (hanya localhost host), atau `MARIADB_CONNECTION_NETWORK` tidak sesuai network tempat client berada | Cek `.env`, cek `docker network inspect <nama_network>` |
| Query ditolak padahal terasa "read-only" | Query tidak diawali salah satu dari `SELECT/FROM/WITH/EXPLAIN/DESCRIBE/SHOW` (misal diawali komentar SQL atau whitespace tidak biasa) | Cek query mentah yang dikirim, pastikan statement pertama benar-benar salah satu keyword yang diizinkan |
| Error `INTERNAL Error` / assertion failure saat query | Kemungkinan `COUNT(*)` langsung di atas view mysql-attached | Hindari `COUNT(*)` langsung; lihat catatan #3 di bagian 9 |
| Heartbeat berhenti muncul di log tapi container masih "running" | Proses Python hang tanpa crash | Restart container (`docker compose restart duckdb-bridge`), investigasi query yang sedang berjalan saat itu |

---

## 11. Menambah Tabel Baru ke Whitelist

1. Cari nama tabel asli di MariaDB (biasanya format `tab<NamaDoctype>` sesuai konvensi Frappe).
2. Tambahkan string tersebut apa adanya ke list `WHITELISTED_TABLES` di `serve.py` (jangan tambahkan prefix `tab` lagi kalau sudah ada di nama).
3. Rebuild & restart:
   ```bash
   docker compose up -d --build
   ```
4. Cek log startup untuk memastikan view baru berhasil dibuat (`view bridge.<nama_baru> siap`).
5. Uji baca lewat `quack_query` atau client SQLMesh sebelum dipakai di production pipeline.

---

## 12. Ringkasan Referensi Cepat

- **Port default**: `9494`
- **Schema view**: `bridge`
- **Auth**: token statis (`QUACK_TOKEN`) + guard regex read-only (`read_only` macro)
- **Sumber data**: MariaDB, di-attach `READ_ONLY` dengan alias `frappe_src`
- **Statement yang diizinkan**: `SELECT`, `FROM`, `WITH`, `EXPLAIN`, `DESCRIBE`, `SHOW`
- **Graceful shutdown**: `SIGTERM`/`SIGINT` → `quack_stop(...)`
- **Healthcheck**: tiap 15 detik, menguji jalur penuh sampai ke MariaDB, bukan cuma proses hidup
- **State**: stateless — semua view scan-through langsung ke MariaDB, aman untuk restart kapan saja
