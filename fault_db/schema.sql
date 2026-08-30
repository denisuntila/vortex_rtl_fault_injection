PRAGMA foreign_keys = ON;


CREATE TABLE IF NOT EXISTS Campaign (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    test_name TEXT NOT NULL,

    cores INTEGER NOT NULL,
    warps INTEGER NOT NULL,
    threads INTEGER NOT NULL,

    start_ts DATETIME,
    end_ts DATETIME,

    UNIQUE(
        test_name,
        cores,
        warps,
        threads
    )
);



CREATE TABLE IF NOT EXISTS Run (

    run_id INTEGER PRIMARY KEY AUTOINCREMENT,

    cid INTEGER NOT NULL,

    instructions INTEGER,
    cycles INTEGER,

    outcome TEXT NOT NULL,

    injection_count INTEGER NOT NULL DEFAULT 0,

    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,


    FOREIGN KEY(cid)
        REFERENCES Campaign(id)
);



CREATE TABLE IF NOT EXISTS Injection (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    run_id INTEGER NOT NULL,

    bit_index INTEGER NOT NULL,

    cycle INTEGER NOT NULL,

    symbol_name TEXT,


    FOREIGN KEY(run_id)
        REFERENCES Run(run_id)
        ON DELETE CASCADE
);



CREATE TABLE IF NOT EXISTS SDC (
    sdc_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,

    expected_v REAL,
    actual_v REAL,
    hamming_dist INTEGER,
    index_value INTEGER,

    FOREIGN KEY (run_id)
        REFERENCES Run(run_id)
        ON DELETE CASCADE
);



CREATE TABLE IF NOT EXISTS Crash (

    run_id INTEGER PRIMARY KEY,

    signal TEXT,

    message TEXT,

    message_size INTEGER DEFAULT 0,


    FOREIGN KEY(run_id)
        REFERENCES Run(run_id)
        ON DELETE CASCADE
);



CREATE INDEX IF NOT EXISTS idx_campaign_lookup
ON Campaign(
    test_name,
    cores,
    warps,
    threads
);



CREATE INDEX IF NOT EXISTS idx_run_campaign
ON Run(cid);



CREATE INDEX IF NOT EXISTS idx_run_outcome
ON Run(outcome);



CREATE INDEX IF NOT EXISTS idx_injection_run
ON Injection(run_id);



CREATE INDEX IF NOT EXISTS idx_sdc_run
ON SDC(run_id);



CREATE INDEX IF NOT EXISTS idx_crash_run
ON Crash(run_id);

