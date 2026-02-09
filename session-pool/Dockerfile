# Use official PnP PowerShell image - handles Linux compatibility correctly
# See: https://pnp.github.io/powershell/articles/docker.html
FROM m365pnp/powershell:nightly

# Install Python and additional PowerShell modules
# PnP image is Debian-based, use apt
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install additional PowerShell modules (PnP.PowerShell already included)
RUN pwsh -Command "Set-PSRepository -Name PSGallery -InstallationPolicy Trusted" && \
    pwsh -Command "Install-Module -Name ExchangeOnlineManagement -Scope AllUsers -Force" && \
    pwsh -Command "Install-Module -Name Az.Accounts -Scope AllUsers -Force" && \
    pwsh -Command "Install-Module -Name MicrosoftTeams -Scope AllUsers -Force"

# Install .NET SDK and PAC CLI for Power Platform (PowerShell module doesn't work on Linux)
RUN curl -sSL https://dot.net/v1/dotnet-install.sh | bash /dev/stdin --channel 8.0 --install-dir /usr/local/dotnet && \
    /usr/local/dotnet/dotnet tool install --global Microsoft.PowerApps.CLI.Tool --version 1.44.2
ENV DOTNET_ROOT=/usr/local/dotnet
ENV PATH="${PATH}:/usr/local/dotnet:/root/.dotnet/tools"

# .NET / PowerShell memory tuning for constrained environments (Mac mini server)
# Workstation GC: collects more often, uses less memory than server GC
ENV DOTNET_gcServer=0
# Hard cap on .NET managed heap at 256MB
ENV DOTNET_GCHeapHardLimit=0x10000000
# Max aggressiveness for memory conservation (0-9 scale)
ENV DOTNET_GCConserveMemory=9
# Tell .NET it's in a container (enables cgroup-aware memory limits)
ENV DOTNET_RUNNING_IN_CONTAINER=true
# Disable diagnostic port, profiler, debugger — saves ~5-10MB per process
ENV DOTNET_EnableDiagnostics=0
# Skip telemetry and first-run experience
ENV DOTNET_NOLOGO=1
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
ENV DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
# Python optimizations
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Create app directory
WORKDIR /app

# Install Python dependencies (no venv needed in container)
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code
COPY session_pool.py .

# Create data directories for token persistence and session state
RUN mkdir -p /data/tokens /app/state

# Expose HTTP API port
EXPOSE 5200

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:5200/health || exit 1

# Override base image entrypoint and run the session pool
ENTRYPOINT []
CMD ["gunicorn", "--bind", "0.0.0.0:5200", "--workers", "1", "--threads", "8", "--timeout", "600", "session_pool:app"]
