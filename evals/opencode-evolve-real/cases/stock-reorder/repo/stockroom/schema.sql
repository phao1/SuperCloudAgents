BEGIN;

CREATE TABLE warehouse (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE supplier (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE item (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE lot (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL REFERENCES item(sku) ON UPDATE CASCADE,
    warehouse TEXT NOT NULL REFERENCES warehouse(code),
    code TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    received TEXT NOT NULL,
    supplier TEXT REFERENCES supplier(code),
    UNIQUE (sku, warehouse, code)
);

CREATE INDEX lot_fifo ON lot (sku, warehouse, received, id);

CREATE TABLE movement (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL REFERENCES item(sku) ON UPDATE CASCADE,
    warehouse TEXT NOT NULL REFERENCES warehouse(code),
    lot_id INTEGER NOT NULL REFERENCES lot(id),
    day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('receive', 'adjust', 'transfer', 'ship')),
    delta INTEGER NOT NULL CHECK (delta <> 0),
    counterpart TEXT REFERENCES warehouse(code),
    memo TEXT NOT NULL DEFAULT ''
);

CREATE INDEX movement_day ON movement (day, id);
CREATE INDEX movement_lot ON movement (lot_id);

CREATE TABLE note (
    id INTEGER PRIMARY KEY,
    lot_id INTEGER NOT NULL REFERENCES lot(id),
    day TEXT NOT NULL,
    text TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
);

CREATE INDEX note_lot ON note (lot_id, day, id);

CREATE TABLE stocktake (
    id INTEGER PRIMARY KEY,
    warehouse TEXT NOT NULL REFERENCES warehouse(code),
    label TEXT NOT NULL,
    opened TEXT NOT NULL,
    closed TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'aborted'))
);

CREATE TABLE observation (
    stocktake_id INTEGER NOT NULL REFERENCES stocktake(id),
    lot_id INTEGER NOT NULL REFERENCES lot(id),
    expected INTEGER NOT NULL CHECK (expected >= 0),
    observed INTEGER CHECK (observed >= 0),
    PRIMARY KEY (stocktake_id, lot_id)
);

CREATE INDEX stocktake_warehouse ON stocktake (warehouse, id);

PRAGMA user_version = 1;
COMMIT;
