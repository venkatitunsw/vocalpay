// Tiny promise wrapper around IndexedDB — just enough to act like the
// SQLite tables in db/schema.sql (one object store per table, key paths
// matching each table's primary key, a couple of indexes for lookups by
// a non-primary column). This is the on-device store for the whole app:
// everything in offline-engine.js reads and writes through here, and
// nothing ever reaches a network.
const DB_NAME = "vocalpay";
const DB_VERSION = 1;

const STORES = {
  users: { key: "user_id" },
  payees: { key: "payee_id", indexes: ["user_id"] },
  payee_payids: { key: "payid_id", indexes: ["payee_id", "value_normalized"] },
  payment_methods: { key: "pm_id", indexes: ["user_id"] },
  transactions: { key: "txn_id", indexes: ["user_id", "session_id"] },
  confirmations: { key: "confirmation_id", indexes: ["txn_id"] },
  audit_events: { key: "event_id", indexes: ["session_id"] },
  payid_directory: { key: "phone_number", indexes: ["email"] },
  bpay_directory: { key: "biller_code" },
  saved_bpay_billers: { key: "biller_id", indexes: ["user_id"] },
};

let _dbPromise = null;

function openDb() {
  if (_dbPromise) return _dbPromise;
  _dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      for (const [name, spec] of Object.entries(STORES)) {
        if (db.objectStoreNames.contains(name)) continue;
        const store = db.createObjectStore(name, { keyPath: spec.key });
        for (const idx of spec.indexes || []) {
          store.createIndex(idx, idx, { unique: false });
        }
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return _dbPromise;
}

function reqToPromise(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function put(store, value) {
  const db = await openDb();
  const tx = db.transaction(store, "readwrite");
  tx.objectStore(store).put(value);
  await reqToPromise(tx.objectStore(store).put(value));
  return value;
}

async function putMany(store, values) {
  const db = await openDb();
  const tx = db.transaction(store, "readwrite");
  const os = tx.objectStore(store);
  for (const v of values) os.put(v);
  await new Promise((resolve, reject) => {
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

async function get(store, key) {
  const db = await openDb();
  const tx = db.transaction(store, "readonly");
  return (await reqToPromise(tx.objectStore(store).get(key))) ?? null;
}

async function getAll(store) {
  const db = await openDb();
  const tx = db.transaction(store, "readonly");
  return (await reqToPromise(tx.objectStore(store).getAll())) || [];
}

async function getAllByIndex(store, indexName, value) {
  const db = await openDb();
  const tx = db.transaction(store, "readonly");
  const idx = tx.objectStore(store).index(indexName);
  return (await reqToPromise(idx.getAll(value))) || [];
}

async function del(store, key) {
  const db = await openDb();
  const tx = db.transaction(store, "readwrite");
  tx.objectStore(store).delete(key);
  await new Promise((resolve, reject) => {
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

async function count(store) {
  const db = await openDb();
  const tx = db.transaction(store, "readonly");
  return await reqToPromise(tx.objectStore(store).count());
}

window.VPDB = { openDb, put, putMany, get, getAll, getAllByIndex, del, count };
