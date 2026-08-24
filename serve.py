import duckdb
import os
import sys
import signal
import logging
import time
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("duckdb-bridge")

# --- Config ---
DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ["DB_PORT"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_DB = os.environ["DB_DB"]

# Token WAJIB tetap (dari env), bukan digenerate ulang tiap restart.
# Kalau random tiap start, healthcheck & SQLMesh client tidak akan pernah
# tahu token yang aktif tanpa baca log container secara manual.
QUACK_TOKEN = os.environ["QUACK_TOKEN"]

# Nama-nama tabel di whitelist SUDAH mengandung prefix "tab" (mengikuti
# konvensi Frappe: "tabPatient", "tabPatient Encounter", dst).
# JANGAN di-prefix ulang di loop bawah -- itu bug yang bikin
# "tabPatient" jadi "tabtabPatient" dan CREATE VIEW gagal.
WHITELISTED_TABLES = [
    "tabPatient",
    "tabHealthcare Practitioner",
    "tabPatient Encounter",
    "tabEncounter SatuSehat",
    "tabCondition SatuSehat",
    "tabMedication",
    "tabItem",
    "tabAllergyIntolerance SatuSehat",
    "tabAllergyIntolerance Validator",
    "tabCarePlan SatuSehat",
    "tabCarePlan Validator",
    "tabClinicalImpression SatuSehat",
    "tabClinicalImpression Validator",
    "tabComposition SatuSehat",
    "tabComposition Validator",
    "tabCondition Validator",
    "tabDiagnosticReport SatuSehat",
    "tabDiagnosticReport Validator",
    "tabEncounter Validator",
    "tabEpisodeOfCare SatuSehat",
    "tabEpisodeOfCare Validator",
    "tabImagingStudy SatuSehat",
    "tabImagingStudy Validator",
    "tabImmunization SatuSehat",
    "tabImmunization Validator",
    "tabMedicationDispense Item SatuSehat",
    "tabMedicationDispense SatuSehat",
    "tabMedicationDispense Validator",
    "tabMedicationRequest Item SatuSehat",
    "tabMedicationRequest SatuSehat",
    "tabMedicationRequest Validator",
    "tabMedicationStatement SatuSehat",
    "tabMedicationStatement Validator",
    "tabObservation SatuSehat",
    "tabObservation Validator",
    "tabProcedure SatuSehat",
    "tabProcedure Validator",
    "tabQuestionnaireResponse SatuSehat",
    "tabQuestionnaireResponse Validator",
    "tabServiceRequest SatuSehat",
    "tabServiceRequest Validator",
    "tabSpecimen SatuSehat",
    "tabSpecimen Validator",
]


def build_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL mysql; LOAD mysql;")
    con.execute("INSTALL quack; LOAD quack;")

    log.info("Menyambung ke MariaDB %s:%s/%s ...", DB_HOST, DB_PORT, DB_DB)
    con.execute(f"""
        ATTACH 'host={DB_HOST} port={DB_PORT} user={DB_USER}
                 password={DB_PASSWORD} database={DB_DB}'
        AS frappe_src (TYPE mysql, READ_ONLY);
    """)

    # Sanity check: ATTACH di DuckDB itu lazy -- tidak benar-benar
    # membuka koneksi & memvalidasi kredensial sampai ada query yang
    # jalan. Query kecil di sini FAIL-FAST kalau host/kredensial salah,
    # daripada baru ketahuan pas SQLMesh jalan nanti.
    con.execute("SELECT 1 FROM frappe_src.information_schema.tables LIMIT 1").fetchone()
    log.info("Koneksi ke MariaDB berhasil.")

    con.execute("CREATE SCHEMA IF NOT EXISTS bridge")

    ready, failed = 0, []
    for t in WHITELISTED_TABLES:
        view = t.lower().replace(" ", "_")
        try:
            con.execute(f'CREATE OR REPLACE VIEW bridge."{view}" AS SELECT * FROM frappe_src."{t}"')
            # NOTE: sengaja TIDAK pakai SELECT count(*) di sini.
            # count(*) di atas view yang men-scan tabel via `mysql` extension
            # memicu bug internal DuckDB (count_star pushdown salah resolve
            # column binding -> INTERNAL Error / assertion failure).
            # LIMIT 1 tetap membuktikan view valid & bisa baca ke MariaDB,
            # tanpa memicu optimasi yang bermasalah itu.
            row = con.execute(f'SELECT * FROM bridge."{view}" LIMIT 1').fetchone()
            status = "ada data" if row is not None else "kosong (0 baris, tapi query valid)"
            log.info("view bridge.%s siap (%s)", view, status)
            ready += 1
        except Exception as e:
            log.error("GAGAL bikin view untuk tabel '%s': %s", t, e)
            failed.append(t)

    log.info("Ringkasan: %s/%s view siap.", ready, len(WHITELISTED_TABLES))
    if failed:
        log.warning("Tabel gagal di-mapping: %s", failed)

    return con


def main():
    con = build_connection()

    con.execute(f"""
        CALL quack_serve('quack:0.0.0.0:9494', allow_other_hostname => true, token => '{QUACK_TOKEN}')
    """)
    con.execute(r"""
        CREATE MACRO read_only(sid, query) AS
            -- \s* di depan: DuckDB trim() TANPA argumen kedua cuma strip
            -- spasi biasa, TIDAK strip newline/tab. Query internal yang
            -- dikirim otomatis oleh Quack client (mis. sinkronisasi
            -- information_schema.schemata saat ATTACH) sering diawali
            -- newline -- tanpa \s* di sini, query legit itu ke-reject.
            regexp_matches(upper(trim(query)), '^\s*(SELECT|FROM|WITH|EXPLAIN|DESCRIBE|SHOW)\b')
    """)
    con.execute("SET GLOBAL quack_authorization_function = 'read_only'")

    log.info("Quack server listening di quack:0.0.0.0:9494 (read-only enforced)")

    log.info("Quack server listening di quack:0.0.0.0:9494 (read-only enforced)")

    # `input()` tidak akan pernah dapat input di container `-d` (stdin
    # tertutup) -- itu langsung EOFError dan container exit begitu proses
    # python fix. Ganti dengan wait loop yang merespons SIGTERM/SIGINT
    # supaya `docker compose down` / restart bisa graceful-shutdown.
    stop = {"flag": False}

    def _handle_signal(signum, _frame):
        log.info("Menerima signal %s, mematikan quack server...", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Heartbeat berkala -- kalau baris ini berhenti muncul di log,
    # berarti proses hang/crash tanpa exit (baru ketahuan dari absennya log).
    last_heartbeat = 0
    while not stop["flag"]:
        time.sleep(1)
        if time.time() - last_heartbeat >= 300:
            log.info("heartbeat: bridge masih hidup")
            last_heartbeat = time.time()

    con.execute("CALL quack_stop('quack:0.0.0.0:9494')")
    log.info("Bridge berhenti.")


if __name__ == "__main__":
    main()