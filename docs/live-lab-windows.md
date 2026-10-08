# Run the Llama demo on Windows

This guide uses **Ubuntu in WSL 2**, with Docker Desktop running Linux containers.
Run PowerShell blocks in PowerShell and Bash blocks in the Ubuntu terminal.
No paid inference key or host Python environment is needed. The model runs locally.

## 1. Install prerequisites (once)

Use a supported Windows 10/11 computer with virtualization enabled. For this full
stack, plan for 16 GB RAM and 30 GB free disk space; allow Docker/WSL about 8 GB RAM.
These are practical demo recommendations, not measured minimum requirements.
Internet access is required for the initial images and model downloads.
See [Docker's Windows requirements](https://docs.docker.com/desktop/setup/install/windows-install/).

Open **PowerShell as Administrator** and run:

```powershell
wsl --install -d Ubuntu
winget install --exact --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
```

Restart Windows if requested. Open **Ubuntu** from the Start menu and finish its
first-run Linux username/password setup. If Ubuntu is already installed, retain it.
In PowerShell, check that Ubuntu uses version 2:

```powershell
wsl --update
wsl --list --verbose
```

If Ubuntu shows version 1, convert it:

```powershell
wsl --set-version Ubuntu 2
```

Open **Docker Desktop**, finish its setup, and wait until its engine is running.
In Docker Desktop settings:

- **General:** enable **Use the WSL 2 based engine**.
- **Resources → WSL Integration:** enable **Ubuntu**, then apply/restart.
- Use **Linux containers**. Do not install a second Docker engine inside Ubuntu.

This is Docker's documented [WSL integration workflow](https://docs.docker.com/desktop/features/wsl/).
If `winget` is unavailable, install Docker Desktop using the official download above.

## 2. Download and start the demo

Open the **Ubuntu terminal** and paste:

```bash
sudo apt-get update && sudo apt-get install -y git python3 openssl
```

Verify Docker connectivity before continuing:

```bash
docker info && docker compose version
```

Then paste:

```bash
mkdir -p ~/projects
cd ~/projects
git clone https://github.com/T-alabdullah/gomodel-lago-exporter.git && cd gomodel-lago-exporter && python3 scripts/run_live.py
```

If the repository requires authentication, use a GitHub account with collaborator
access. GitHub HTTPS authentication requires a token or credential manager, not
your account password; do not put tokens in the clone URL.

Keep the repository in Ubuntu's filesystem (`~/projects`), not `/mnt/c`.
The startup script creates local credentials, builds containers, starts databases
and Lago, downloads `llama3.2:1b`, and provisions the demo. First startup can take
several minutes or longer depending on downloads and hardware. Let it finish.
The model download is approximately 1.3 GB; container images require additional space.

Open **http://localhost:8090** in your Windows browser. The demo binds to localhost;
each machine runs its own independent database and request history.

## 3. Verify the first request

- Open **Live Chat**, select **Acme** and **ollama-qai**, and send “Say hello.”
- Wait for Llama's answer and the persisted source row in **Request Trace**.
- Delivery starts paused on a fresh installation. Click **Prepare only**, then
  **Deliver / replay** to observe the delivery and Lago events.
- Inspect **Databases** and **Lago Billing**. Wait at least two seconds and click
  **Reconcile**. The [full guide](live-lab.md#guided-experiment) covers failure,
  dead-letter, duplicate replay and automatic delivery demonstrations.

## 4. Stop, restart, or update

Stop the stack from the Ubuntu terminal (data and model weights are retained):

```bash
cd ~/projects/gomodel-lago-exporter
docker compose --env-file .env.demo -f docker-compose.yml -f docker-compose.live.yml stop
```

Restart later, with Docker Desktop running:

```bash
cd ~/projects/gomodel-lago-exporter
python3 scripts/run_live.py
```

Download future updates and rebuild:

```bash
cd ~/projects/gomodel-lago-exporter
git pull --ff-only && python3 scripts/run_live.py
```

Keep `.env.demo` with this installation; it contains the credentials for its
persistent volumes. Never commit or share it. Do not use `down -v` unless you
intend to erase demo databases and downloaded model weights. Pause/fault settings
persist across restarts: restore delivery and choose the desired mode in Settings.

## Troubleshooting

- **`docker: command not found` / cannot connect:** start Docker Desktop and enable
  Ubuntu WSL integration, then reopen the Ubuntu terminal.
- **Port already allocated:** stop the other application using the reported port.
  The lab uses 8090; the stack also publishes the ports in `docker-compose.yml`.
- **Startup timed out:** inspect the logs below, check available memory/disk, and
  rerun `python3 scripts/run_live.py` after fixing the reported problem. It retains
  existing credentials and volumes.
- **Slow answers:** CPU inference is expected; use a short prompt and a low output
  limit. A GPU is not required or configured by this demo.

```bash
cd ~/projects/gomodel-lago-exporter
docker compose --env-file .env.demo -f docker-compose.yml -f docker-compose.live.yml ps
docker compose --env-file .env.demo -f docker-compose.yml -f docker-compose.live.yml logs --tail=100 live-lab gomodel ollama lago-api
```

The live stack has been exercised on macOS Docker Desktop. These Windows steps
use the same Linux containers through WSL 2; native Windows execution has not
been tested in this development environment.
