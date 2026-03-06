#################################################################################
# compose/mssql/Dockerfile                                                      #
#                                                                               #
# Thin wrapper around the official SQL Server 2022 image that:                  #
#   1. Installs bash (already present in the base image).                       #
#   2. Copies the database-init entrypoint script.                              #
#   3. Sets it as the container entrypoint so test_db is created automatically. #
#                                                                               #
# Build:                                                                        #
#   docker build -t ft_mssql:2022 mssql/                                        #
#                                                                               #
# Why a custom image instead of a command override?                             #
#   • SQL Server's official image uses a C-binary entrypoint, not a shell       #
#     script.  Replacing CMD with a bash one-liner loses proper signal          #
#     forwarding, so sqlservr does not receive SIGTERM on `docker stop`.        #
#   • A dedicated ENTRYPOINT script lets us run the CREATE DATABASE T-SQL       #
#     cleanly and then exec sqlservr as PID 1 with correct signal semantics.    #
#   • The `build:` key in docker-compose keeps the wrapper self-contained;      #
#     no registry push is required.                                             #
#################################################################################
FROM mcr.microsoft.com/mssql/server:2022-latest

# Switch to root to copy files; MSSQL image drops back to mssql user at runtime
USER root

COPY entrypoint.sh /opt/mssql-init/entrypoint.sh
RUN chmod +x /opt/mssql-init/entrypoint.sh

# Restore the non-root MSSQL user expected by the base image
USER mssql

ENTRYPOINT ["/opt/mssql-init/entrypoint.sh"]
