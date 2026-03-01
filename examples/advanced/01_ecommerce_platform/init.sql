-- Allow the postgres superuser to create per-tenant databases.
-- In production use a dedicated service account with CREATEDB privilege.
ALTER USER postgres CREATEDB;

-- Optional: create a role for the application that has limited access.
-- CREATE ROLE app_user WITH LOGIN PASSWORD 'app_password' CREATEDB;
-- GRANT ALL ON DATABASE saas_platform TO app_user;
