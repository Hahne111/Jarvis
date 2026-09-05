-- Wird nur beim ersten Start des Datenvolumens ausgeführt (docker-entrypoint-initdb.d).
-- Schema-Migrationen folgen ab Phase 1 (Commit 003/004) über ein Migrations-Tool, nicht hier.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
