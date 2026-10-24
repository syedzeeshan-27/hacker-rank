-- Synthetic fixture for the WS-D custom evaluation cases (fictional people, example.org domain)
-- Dialect: SQLite-compatible SQL
-- Captured at: 2026-09-15T12:00:00Z
-- Database topology: failover_replica

BEGIN TRANSACTION;
CREATE TABLE customer_profiles_failover_replica (
                resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
                account_status TEXT NOT NULL, updated_at TEXT NOT NULL
            );
INSERT INTO "customer_profiles_failover_replica" VALUES('account_9004','user_904','Lucia Ferro','lucia.ferro@example.org','+39 347 000 9004','ACTIVE','2026-09-15T11:45:00Z');
INSERT INTO "customer_profiles_failover_replica" VALUES('account_9104','user_9104','Karl Ostrom','karl.ostrom@example.org','+46 70 000 9104','ACTIVE','2026-09-15T11:45:00Z');
CREATE TABLE customer_profiles_primary (
                resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
                account_status TEXT NOT NULL, updated_at TEXT NOT NULL
            );
INSERT INTO "customer_profiles_primary" VALUES('account_9104','user_9104','Karl Ostrom','karl.ostrom@example.org','+46 70 000 9104','ACTIVE','2026-09-15T11:45:00Z');
COMMIT;
