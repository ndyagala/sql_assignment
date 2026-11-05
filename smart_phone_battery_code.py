"""
SMARTPHONE BATTERY PERFORMANCE TESTING

Tables:
    phone_models:       20 rows
    battery_samples:   100 rows
    test_sessions:     400 rows
    measurements:    4,040 rows

Measurement types:
    Nominal: operating_system and workload
    Ordinal: battery_health_band — Poor < Fair < Good
    Interval: temperature_c
    Ratio: runtime_minutes, capacity and cycle_count

Composite primary keys:
    test_sessions: (sample_id, session_number)
    measurements: (sample_id, session_number, reading_number)

Deliberate imperfections:
    120 missing temperature readings
    40 extra duplicate measurement events
"""

import math
import random
import sqlite3
import tempfile

from datetime import datetime, timedelta
from pathlib import Path
from google.colab import files


# ============================================================
# 1. Reproducible configuration
# ============================================================

SEED = 3398
rng = random.Random(SEED)

MODEL_COUNT = 20
SAMPLE_COUNT = 100
READINGS_PER_SESSION = 10

MISSING_TEMPERATURE_COUNT = 120
DUPLICATE_COUNT = 40

WORKLOADS = ["Browsing", "Video", "Gaming", "Standby"]

# Fictional approximate discharge-current ranges, in mA.
CURRENT_RANGES = {
    "Browsing": (400, 700),
    "Video": (500, 850),
    "Gaming": (1100, 1700),
    "Standby": (50, 100)
}

# Fresh output folder protects previous runs.
OUTPUT_DIR = Path(
    tempfile.mkdtemp(prefix="smartphone_battery_", dir="/content")
)
DATABASE_PATH = OUTPUT_DIR / "smartphone_battery.db"


def timestamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def timestamp_check(column):
    return f"""
        length({column}) = 19
        AND {column} GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
        AND datetime({column}, '+0 days') IS NOT NULL
        AND datetime({column}, '+0 days') = {column}
    """


def health_band(capacity, rated_capacity):
    """
    Fictional thresholds:
        Good: capacity retention >= 90%
        Fair: capacity retention >= 80% and < 90%
        Poor: capacity retention < 80%
    """
    retention = capacity / rated_capacity

    if retention >= 0.90:
        return "Good"
    if retention >= 0.80:
        return "Fair"
    return "Poor"


# ============================================================
# 2. Schema
# ============================================================

SCHEMA = f"""
CREATE TABLE phone_models (
    model_id INTEGER PRIMARY KEY,

    model_name TEXT NOT NULL UNIQUE
        CHECK (length(trim(model_name)) > 0),

    operating_system TEXT NOT NULL
        CHECK (operating_system IN ('Android', 'iOS')),

    rated_capacity_mah INTEGER NOT NULL
        CHECK (
            typeof(rated_capacity_mah) = 'integer'
            AND rated_capacity_mah BETWEEN 2000 AND 7000
        ),

    nominal_voltage_v REAL NOT NULL
        CHECK (
            typeof(nominal_voltage_v) IN ('integer', 'real')
            AND nominal_voltage_v BETWEEN 3.5 AND 4.0
        )
);

CREATE TABLE battery_samples (
    sample_id INTEGER PRIMARY KEY,

    model_id INTEGER NOT NULL,

    sample_code TEXT NOT NULL UNIQUE
        CHECK (length(trim(sample_code)) > 0),

    cycle_count INTEGER NOT NULL
        CHECK (
            typeof(cycle_count) = 'integer'
            AND cycle_count BETWEEN 0 AND 2000
        ),

    measured_capacity_mah REAL NOT NULL
        CHECK (
            typeof(measured_capacity_mah) IN ('integer', 'real')
            AND measured_capacity_mah > 0
        ),

    battery_health_band TEXT NOT NULL
        CHECK (battery_health_band IN ('Poor', 'Fair', 'Good')),

    FOREIGN KEY (model_id)
        REFERENCES phone_models(model_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE test_sessions (
    sample_id INTEGER NOT NULL,

    session_number INTEGER NOT NULL
        CHECK (
            typeof(session_number) = 'integer'
            AND session_number > 0
        ),

    workload TEXT NOT NULL
        CHECK (workload IN ('Browsing', 'Video', 'Gaming', 'Standby')),

    started_at TEXT NOT NULL
        CHECK ({timestamp_check('started_at')}),

    ambient_temperature_c REAL NOT NULL
        CHECK (
            typeof(ambient_temperature_c) IN ('integer', 'real')
            AND ambient_temperature_c BETWEEN 10 AND 40
        ),

    starting_charge_percent REAL NOT NULL
        CHECK (starting_charge_percent = 100),

    ending_charge_percent REAL NOT NULL
        CHECK (ending_charge_percent = 20),

    runtime_minutes REAL NOT NULL
        CHECK (
            typeof(runtime_minutes) IN ('integer', 'real')
            AND runtime_minutes > 0
        ),

    PRIMARY KEY (sample_id, session_number),

    UNIQUE (sample_id, workload),

    FOREIGN KEY (sample_id)
        REFERENCES battery_samples(sample_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE measurements (
    sample_id INTEGER NOT NULL,
    session_number INTEGER NOT NULL,

    reading_number INTEGER NOT NULL
        CHECK (
            typeof(reading_number) = 'integer'
            AND reading_number > 0
        ),

    elapsed_minutes REAL NOT NULL
        CHECK (
            typeof(elapsed_minutes) IN ('integer', 'real')
            AND elapsed_minutes >= 0
        ),

    temperature_c REAL
        CHECK (
            temperature_c IS NULL
            OR (
                typeof(temperature_c) IN ('integer', 'real')
                AND temperature_c BETWEEN -20 AND 100
            )
        ),

    voltage_v REAL NOT NULL
        CHECK (
            typeof(voltage_v) IN ('integer', 'real')
            AND voltage_v BETWEEN 2.5 AND 4.5
        ),

    current_ma REAL NOT NULL
        CHECK (
            typeof(current_ma) IN ('integer', 'real')
            AND current_ma > 0
        ),

    charge_percent REAL NOT NULL
        CHECK (
            typeof(charge_percent) IN ('integer', 'real')
            AND charge_percent BETWEEN 0 AND 100
        ),

    notes TEXT
        CHECK (notes IS NULL OR length(trim(notes)) > 0),

    PRIMARY KEY (sample_id, session_number, reading_number),

    FOREIGN KEY (sample_id, session_number)
        REFERENCES test_sessions(sample_id, session_number)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX idx_samples_model ON battery_samples(model_id);
CREATE INDEX idx_session_date ON test_sessions(started_at);

CREATE INDEX idx_measurement_elapsed
    ON measurements(sample_id, session_number, elapsed_minutes);
"""


# ============================================================
# 3. Cross-table rules
# ============================================================

RULES = """
CREATE VIEW business_rule_violations AS

SELECT 'Measured capacity exceeds rated capacity' AS violation
FROM battery_samples b
JOIN phone_models p ON p.model_id = b.model_id
WHERE b.measured_capacity_mah > p.rated_capacity_mah

UNION ALL

SELECT 'Incorrect battery health band'
FROM battery_samples b
JOIN phone_models p ON p.model_id = b.model_id
WHERE b.battery_health_band !=
    CASE
        WHEN b.measured_capacity_mah / p.rated_capacity_mah >= 0.90
        THEN 'Good'
        WHEN b.measured_capacity_mah / p.rated_capacity_mah >= 0.80
        THEN 'Fair'
        ELSE 'Poor'
    END

UNION ALL

SELECT 'Measurement exceeds session runtime'
FROM measurements m
JOIN test_sessions s
  ON s.sample_id = m.sample_id
 AND s.session_number = m.session_number
WHERE m.elapsed_minutes > s.runtime_minutes + 0.000001

UNION ALL

SELECT 'Charge inconsistent with constant-current simulation'
FROM measurements m
JOIN test_sessions s
  ON s.sample_id = m.sample_id
 AND s.session_number = m.session_number
WHERE abs(
    m.charge_percent -
    (
        s.starting_charge_percent
        - (s.starting_charge_percent - s.ending_charge_percent)
          * m.elapsed_minutes / s.runtime_minutes
    )
) > 0.011

UNION ALL

SELECT 'Current inconsistent with session runtime'
FROM measurements m
JOIN test_sessions s
  ON s.sample_id = m.sample_id
 AND s.session_number = m.session_number
JOIN battery_samples b ON b.sample_id = s.sample_id
WHERE abs(
    s.runtime_minutes -
    (
        b.measured_capacity_mah
        * (s.starting_charge_percent - s.ending_charge_percent)
        / 100.0 / m.current_ma * 60.0
    )
) > 0.0001

UNION ALL

SELECT 'Sessions overlap or lack recharge interval'
FROM test_sessions earlier
JOIN test_sessions later
  ON earlier.sample_id = later.sample_id
 AND earlier.session_number < later.session_number
WHERE julianday(later.started_at) <
      julianday(earlier.started_at)
      + earlier.runtime_minutes / 1440.0
      + 4.0 / 24.0;
"""


# ============================================================
# 4. Generate phone models
# ============================================================

models = []

for model_id in range(1, MODEL_COUNT + 1):
    operating_system = (
        "iOS" if model_id % 4 == 0 else "Android"
    )

    models.append((
        model_id,
        f"SimPhone-{model_id:02d}",
        operating_system,
        rng.choice([3000, 3500, 4000, 4500, 5000, 5500]),
        rng.choice([3.7, 3.8, 3.85])
    ))

model_lookup = {row[0]: row for row in models}


# ============================================================
# 5. Generate individual battery samples
# ============================================================

samples = []

for sample_id in range(1, SAMPLE_COUNT + 1):
    # Exactly five physical samples per model.
    model_id = (sample_id - 1) // 5 + 1
    rated_capacity = model_lookup[model_id][3]

    cycle_count = rng.randint(0, 1000)

    # Capacity generally decreases with cycle count, with variation.
    retention = max(
        0.62,
        min(
            0.995,
            0.995 - 0.00028 * cycle_count + rng.gauss(0, 0.015)
        )
    )

    measured_capacity = round(rated_capacity * retention, 2)

    samples.append((
        sample_id,
        model_id,
        f"SIM-BAT-{sample_id:04d}",
        cycle_count,
        measured_capacity,
        health_band(measured_capacity, rated_capacity)
    ))


# ============================================================
# 6. Generate sessions and original measurements
# ============================================================

sessions = []
measurements = []

for sample in samples:
    sample_id = sample[0]
    measured_capacity = sample[4]

    # Different samples may be tested simultaneously.
    next_start = datetime(2025, 1, 1, 9) + timedelta(
        days=rng.randint(0, 30)
    )

    workload_order = WORKLOADS.copy()
    rng.shuffle(workload_order)

    for session_number, workload in enumerate(workload_order, start=1):
        low_current, high_current = CURRENT_RANGES[workload]

        # Constant current within each session keeps runtime and
        # charge measurements mathematically consistent.
        current_ma = round(rng.uniform(low_current, high_current), 2)

        runtime = measured_capacity * 0.80 / current_ma * 60.0
        ambient = round(rng.uniform(20, 27), 2)

        sessions.append((
            sample_id,
            session_number,
            workload,
            timestamp(next_start),
            ambient,
            100.0,
            20.0,
            runtime
        ))

        for reading_number in range(1, READINGS_PER_SESSION + 1):
            fraction = (
                (reading_number - 1)
                / (READINGS_PER_SESSION - 1)
            )

            elapsed = runtime * fraction

            # Simplified linear discharge under constant current.
            charge = round(100 - 80 * fraction, 2)

            # Simplified voltage curve, not a real electrochemical model.
            voltage = round(
                4.20 - 0.65 * fraction + rng.gauss(0, 0.008),
                3
            )

            # Workload current determines approximate warming.
            temperature_rise = 1.0 + current_ma / 100.0

            temperature = round(
                ambient
                + temperature_rise * (1 - math.exp(-4 * fraction))
                + rng.gauss(0, 0.25),
                2
            )

            measurements.append((
                sample_id,
                session_number,
                reading_number,
                elapsed,
                temperature,
                voltage,
                current_ma,
                charge,
                None
            ))

        # At least four hours for recharge and cooling.
        next_start = next_start + timedelta(
            seconds=math.ceil(runtime * 60),
            hours=6
        )

# Cycle count and measured capacity represent baseline condition.
# Four sessions per sample assume negligible additional degradation.


# ============================================================
# 7. Deliberate missing and duplicate data
# ============================================================

duplicate_sources = rng.sample(
    range(len(measurements)),
    DUPLICATE_COUNT
)
duplicate_source_set = set(duplicate_sources)

missing_candidates = [
    index
    for index in range(len(measurements))
    if index not in duplicate_source_set
]

missing_temperature_indices = rng.sample(
    missing_candidates,
    MISSING_TEMPERATURE_COUNT
)

for index in missing_temperature_indices:
    record = list(measurements[index])
    record[4] = None
    record[8] = "Temperature sensor unavailable."
    measurements[index] = tuple(record)

# Duplicate sources retain complete temperatures so the final
# missing-temperature count stays exactly 120.
next_reading = {
    (session[0], session[1]): READINGS_PER_SESSION
    for session in sessions
}

for source_index in duplicate_sources:
    record = list(measurements[source_index])
    key = (record[0], record[1])

    next_reading[key] += 1
    record[2] = next_reading[key]

    measurements.append(tuple(record))

# Reading numbers identify stored records.
# elapsed_minutes determines observation order.
# Duplicate events may share elapsed time without sharing a primary key.


# ============================================================
# 8. Create and populate the database
# ============================================================

connection = sqlite3.connect(DATABASE_PATH)

try:
    connection.execute("PRAGMA foreign_keys = ON")

    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("Foreign-key enforcement is disabled.")

    connection.executescript(SCHEMA)
    connection.executescript(RULES)

    # Protect cross-table rules on relevant child and parent changes.
    for table in (
        "phone_models",
        "battery_samples",
        "test_sessions",
        "measurements"
    ):
        for operation in ("INSERT", "UPDATE"):
            connection.execute(f"""
                CREATE TRIGGER validate_{table}_{operation.lower()}
                AFTER {operation} ON {table}
                BEGIN
                    SELECT CASE WHEN EXISTS (
                        SELECT 1 FROM business_rule_violations
                    )
                    THEN RAISE(
                        ABORT,
                        'Battery testing business rule violated'
                    )
                    END;
                END;
            """)

    with connection:
        connection.executemany(
            "INSERT INTO phone_models VALUES (?, ?, ?, ?, ?)",
            models
        )

        connection.executemany(
            "INSERT INTO battery_samples VALUES (?, ?, ?, ?, ?, ?)",
            samples
        )

        connection.executemany(
            "INSERT INTO test_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            sessions
        )

        connection.executemany(
            """
            INSERT INTO measurements
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            measurements
        )


    # ========================================================
    # 9. Validate row counts, structure and keys
    # ========================================================

    def scalar(sql, parameters=()):
        return connection.execute(sql, parameters).fetchone()[0]


    expected_counts = {
        "phone_models": 20,
        "battery_samples": 100,
        "test_sessions": 400,
        "measurements": 4040
    }

    composite_keys = {}

    print("TABLE COUNTS, DATA TYPES AND KEYS\n")

    for table, expected in expected_counts.items():
        actual = scalar(f"SELECT COUNT(*) FROM {table}")

        if actual != expected:
            raise RuntimeError(f"Incorrect row count: {table}")

        columns = connection.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()

        primary_key = [
            column[1]
            for column in sorted(columns, key=lambda item: item[5])
            if column[5] > 0
        ]

        if not primary_key:
            raise RuntimeError(f"Missing primary key: {table}")

        if len(primary_key) > 1:
            composite_keys[table] = primary_key

            if any(
                column[5] > 0 and column[3] != 1
                for column in columns
            ):
                raise RuntimeError("Nullable composite-key component.")

        print(f"{table}: {actual:,} rows")
        print(
            "Columns:",
            ", ".join(
                f"{column[1]} ({column[2]})"
                for column in columns
            )
        )
        print("Primary key:", ", ".join(primary_key))

        for fk in connection.execute(
            f"PRAGMA foreign_key_list({table})"
        ):
            print(f"Foreign key: {fk[3]} → {fk[2]}.{fk[4]}")

        print()

    if len(composite_keys) != 2:
        raise RuntimeError("Expected two composite primary keys.")

    if len(connection.execute(
        "PRAGMA table_info(measurements)"
    ).fetchall()) != 9:
        raise RuntimeError("Unexpected measurement column count.")

    if scalar("SELECT COUNT(*) FROM business_rule_violations"):
        raise RuntimeError("Business-rule violation detected.")

    if scalar("""
        SELECT COUNT(*)
        FROM phone_models p
        WHERE (
            SELECT COUNT(*) FROM battery_samples b
            WHERE b.model_id = p.model_id
        ) != 5
    """):
        raise RuntimeError("Expected five samples per model.")

    if scalar("""
        SELECT COUNT(*)
        FROM battery_samples b
        WHERE (
            SELECT COUNT(*) FROM test_sessions s
            WHERE s.sample_id = b.sample_id
        ) != 4
    """):
        raise RuntimeError("Expected four sessions per sample.")


    # ========================================================
    # 10. Validate missingness, duplicates and endpoints
    # ========================================================

    missing_temperatures = scalar("""
        SELECT COUNT(*) FROM measurements
        WHERE temperature_c IS NULL
    """)

    missing_notes = scalar("""
        SELECT COUNT(*) FROM measurements
        WHERE notes IS NULL
    """)

    duplicate_groups, extra_duplicates = connection.execute("""
        SELECT COUNT(*), COALESCE(SUM(copies - 1), 0)
        FROM (
            SELECT COUNT(*) AS copies
            FROM measurements
            GROUP BY sample_id, session_number, elapsed_minutes,
                     temperature_c, voltage_v, current_ma,
                     charge_percent, notes
            HAVING COUNT(*) > 1
        )
    """).fetchone()

    if missing_temperatures != MISSING_TEMPERATURE_COUNT:
        raise RuntimeError("Missing temperature count mismatch.")

    if (
        duplicate_groups != DUPLICATE_COUNT
        or extra_duplicates != DUPLICATE_COUNT
    ):
        raise RuntimeError("Duplicate event count mismatch.")

    # Remove duplicate ingestion when checking original observation sets.
    invalid_sessions = scalar("""
        WITH events AS (
            SELECT DISTINCT
                   sample_id, session_number, elapsed_minutes,
                   temperature_c, voltage_v, current_ma,
                   charge_percent, notes
            FROM measurements
        )
        SELECT COUNT(*) FROM (
            SELECT s.sample_id, s.session_number
            FROM test_sessions s
            LEFT JOIN events e
              ON e.sample_id = s.sample_id
             AND e.session_number = s.session_number
            GROUP BY s.sample_id, s.session_number
            HAVING COUNT(e.elapsed_minutes) != 10
                OR MIN(e.elapsed_minutes) != 0
                OR abs(MAX(e.elapsed_minutes) - s.runtime_minutes) > 0.000001
                OR abs(MAX(e.charge_percent) - 100) > 0.001
                OR abs(MIN(e.charge_percent) - 20) > 0.001
        )
    """)

    if invalid_sessions:
        raise RuntimeError("Session observation endpoints are invalid.")

    health_categories = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT battery_health_band FROM battery_samples"
        )
    }

    if health_categories != {"Poor", "Fair", "Good"}:
        raise RuntimeError("Not all ordinal categories represented.")

    print("MISSING AND DUPLICATE DATA\n")

    print(
        f"Missing temperatures: {missing_temperatures}/4000 "
        f"({missing_temperatures / 4000 * 100:.2f}% of original readings)"
    )
    print(
        f"Missing temperatures: {missing_temperatures}/4040 "
        f"({missing_temperatures / 4040 * 100:.2f}% of final readings)"
    )
    print(f"Duplicate event groups: {duplicate_groups}")
    print(
        f"Extra duplicate rows: {extra_duplicates}/4040 "
        f"({extra_duplicates / 4040 * 100:.2f}%)"
    )
    print(
        f"Optional notes without comments: {missing_notes}/4040 "
        f"({missing_notes / 4040 * 100:.2f}%)"
    )


    # ========================================================
    # 11. Test invalid operations — always roll back
    # ========================================================

    def expect_rejection(name, sql, parameters=()):
        connection.execute("SAVEPOINT constraint_test")

        try:
            connection.execute(sql, parameters)

        except sqlite3.IntegrityError:
            print(f"PASS: {name}")

        else:
            raise RuntimeError(
                f"Invalid operation unexpectedly accepted: {name}"
            )

        finally:
            connection.execute("ROLLBACK TO constraint_test")
            connection.execute("RELEASE constraint_test")


    print("\nCONSTRAINT TESTS\n")

    rejection_tests = [
        (
            "Missing required model name",
            """
            INSERT INTO phone_models
            VALUES (9999, NULL, 'Android', 4000, 3.8)
            """
        ),
        (
            "Unique model name",
            """
            INSERT INTO phone_models
            SELECT 9999, model_name, operating_system,
                   rated_capacity_mah, nominal_voltage_v
            FROM phone_models WHERE model_id = 1
            """
        ),
        (
            "Invalid model foreign key",
            """
            INSERT INTO battery_samples
            VALUES (9999, 9999, 'INVALID-SAMPLE', 0, 3900, 'Good')
            """
        ),
        (
            "Duplicate session composite key",
            """
            INSERT INTO test_sessions
            SELECT * FROM test_sessions LIMIT 1
            """
        ),
        (
            "Duplicate measurement composite key",
            """
            INSERT INTO measurements
            SELECT * FROM measurements LIMIT 1
            """
        ),
        (
            "Invalid composite foreign key",
            """
            INSERT INTO measurements
            VALUES (1, 999, 1, 0, 25, 4.2, 500, 100, NULL)
            """
        ),
        (
            "Negative cycle count",
            """
            UPDATE battery_samples
            SET cycle_count = -1 WHERE sample_id = 1
            """
        ),
        (
            "Capacity above rated capacity",
            """
            UPDATE battery_samples
            SET measured_capacity_mah = 99999 WHERE sample_id = 1
            """
        ),
        (
            "Invalid health category",
            """
            UPDATE battery_samples
            SET battery_health_band = 'Excellent' WHERE sample_id = 1
            """
        ),
        (
            "Charge above 100 percent",
            """
            UPDATE measurements
            SET charge_percent = 101
            WHERE sample_id = 1 AND session_number = 1
              AND reading_number = 1
            """
        ),
        (
            "Negative current",
            """
            UPDATE measurements
            SET current_ma = -1
            WHERE sample_id = 1 AND session_number = 1
              AND reading_number = 1
            """
        ),
        (
            "Measurement after session end",
            """
            UPDATE measurements
            SET elapsed_minutes = 999999
            WHERE sample_id = 1 AND session_number = 1
              AND reading_number = 1
            """
        ),
        (
            "Invalid session timestamp",
            """
            UPDATE test_sessions
            SET started_at = '2025-02-30 09:00:00'
            WHERE sample_id = 1 AND session_number = 1
            """
        ),
        (
            "Overlapping sessions",
            """
            UPDATE test_sessions
            SET started_at = (
                SELECT started_at FROM test_sessions
                WHERE sample_id = 1 AND session_number = 1
            )
            WHERE sample_id = 1 AND session_number = 2
            """
        )
    ]

    for name, sql in rejection_tests:
        expect_rejection(name, sql)

    existing_band = scalar("""
        SELECT battery_health_band FROM battery_samples
        WHERE sample_id = 1
    """)

    incorrect_band = "Poor" if existing_band != "Poor" else "Good"

    expect_rejection(
        "Health band inconsistent with capacity",
        """
        UPDATE battery_samples
        SET battery_health_band = ?
        WHERE sample_id = 1
        """,
        (incorrect_band,)
    )


    # ========================================================
    # 12. Final integrity checks and retrieval
    # ========================================================

    integrity = [
        row[0]
        for row in connection.execute("PRAGMA integrity_check")
    ]

    foreign_key_errors = connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()

    if integrity != ["ok"] or foreign_key_errors:
        raise RuntimeError("Database integrity validation failed.")

    if scalar("SELECT COUNT(*) FROM business_rule_violations"):
        raise RuntimeError("Business-rule validation failed.")

    for table, expected in expected_counts.items():
        if scalar(f"SELECT COUNT(*) FROM {table}") != expected:
            raise RuntimeError("Constraint tests changed row counts.")

    print("\nFINAL VALIDATION\n")
    print("Integrity check: ok")
    print("Foreign-key violations: 0")
    print("Business-rule violations: 0")
    print("Four linked tables")
    print("Two composite primary keys")
    print("Measurements: 9 columns and 4,040 rows")
    print("All four measurement types represented")
    print("All 15 invalid-operation tests rejected")
    print("Missing and duplicate records retained")

    print("\nBATTERY HEALTH COUNTS\n")

    for band, count in connection.execute("""
        SELECT battery_health_band, COUNT(*)
        FROM battery_samples
        GROUP BY battery_health_band
        ORDER BY CASE battery_health_band
            WHEN 'Poor' THEN 1
            WHEN 'Fair' THEN 2
            ELSE 3
        END
    """):
        print(f"{band}: {count}")

    print("\nRUNTIME BY WORKLOAD\n")

    for workload, count, mean_runtime in connection.execute("""
        SELECT workload, COUNT(*), ROUND(AVG(runtime_minutes), 2)
        FROM test_sessions
        GROUP BY workload
        ORDER BY workload
    """):
        print(
            f"{workload}: {count} sessions, "
            f"mean runtime {mean_runtime:.2f} minutes"
        )

    print("\nTEMPERATURE BY WORKLOAD — DUPLICATE EVENTS EXCLUDED\n")

    for workload, mean_temperature in connection.execute("""
        WITH distinct_events AS (
            SELECT DISTINCT
                   sample_id, session_number, elapsed_minutes,
                   temperature_c, voltage_v, current_ma,
                   charge_percent, notes
            FROM measurements
        )
        SELECT s.workload, ROUND(AVG(m.temperature_c), 2)
        FROM distinct_events m
        JOIN test_sessions s
          ON s.sample_id = m.sample_id
         AND s.session_number = m.session_number
        GROUP BY s.workload
        ORDER BY s.workload
    """):
        print(f"{workload}: {mean_temperature:.2f} °C")

    connection.commit()

finally:
    connection.close()


# ============================================================
# 13. Reopen saved database and download only the DB file
# ============================================================

with sqlite3.connect(DATABASE_PATH) as saved:
    saved.execute("PRAGMA foreign_keys = ON")

    if saved.execute(
        "PRAGMA integrity_check"
    ).fetchone()[0] != "ok":
        raise RuntimeError("Saved database integrity check failed.")

    if saved.execute(
        "SELECT COUNT(*) FROM measurements"
    ).fetchone()[0] != 4040:
        raise RuntimeError("Saved measurement count mismatch.")

    if saved.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("Saved database has foreign-key violations.")

print("\nDatabase saved and reopened successfully.")
print("Downloading smartphone_battery.db ...")

files.download(str(DATABASE_PATH))