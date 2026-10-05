-- SeaVix local infra init. Runs once on first boot of a fresh pgdata volume
-- (postgres image convention: files in /docker-entrypoint-initdb.d).
-- OpenFGA needs its own database; the postgres image only auto-creates
-- POSTGRES_DB (seavix), which is reserved for the application services.
CREATE DATABASE openfga;
