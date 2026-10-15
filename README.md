# slurm-mcp

Use **Claude Code** (or any [MCP](https://modelcontextprotocol.io/) client) to work on a
**SLURM HPC cluster** over SSH: browse and edit files, write and submit batch jobs, watch
the queue, read logs and debug failed jobs. You do all of this in plain language, from
your laptop.

```
> Look at /scratch/me/rnaseq/fastq, write a job that runs fastqc on every sample with 8 cores,
  and submit it.

● hpc_find_files(base_path="/scratch/me/rnaseq/fastq", name_pattern="*.fastq.gz")
● hpc_generate_sbatch(job_name="fastqc", cpus=8, memory_gb=16, walltime="02:00:00", ...)
  Here is the script. Shall I save and submit it?
> yes
● hpc_write_file(path="/scratch/me/rnaseq/jobs/fastqc.sbatch", ...)
● hpc_submit_job(script_path="/scratch/me/rnaseq/jobs/fastqc.sbatch")
  Submitted as job 481516. I'll check the log when it's running.
```

The server works with any cluster. Site details such as storage paths, partitions, limits,
QoS, environment setup and local rules live in a small `cluster.toml` profile plus a folder
of Markdown notes that Claude reads before it suggests resources.

---

## Contents

- [How it works](#how-it-works)
- [What Claude can do](#what-claude-can-do)
- [Safety model](#safety-model)
- [Setup](#setup)
  1. [SSH access to the cluster](#1-ssh-access-to-the-cluster)
  2. [Get the code and create `.env`](#2-get-the-code-and-create-env)
  3. [Describe your cluster](#3-describe-your-cluster)
  4. [Build and test](#4-build-and-test)
  5. [Connect Claude Code](#5-connect-claude-code)
  6. [Optional: skip prompts for read-only tools](#6-optional-skip-prompts-for-read-only-tools)
- [Using it](#using-it)
- [Configuration reference](#configuration-reference)
- [Other MCP clients](#other-mcp-clients)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## How it works

```
 your laptop                                              the cluster
┌──────────────────────────────────────────┐            ┌───────────────────────────┐
│ Claude Code                              │            │ login node                │
│   │ MCP over stdio                       │    SSH     │   squeue, sbatch, sacct,  │
│   ▼                                      │ ─────────► │   ls, cat, grep, …        │
│ slurm-mcp (Docker container or uv)       │ (optional  │                           │
│   reads .env + cluster/cluster.toml      │ jump host) │ compute nodes ◄── SLURM   │
└──────────────────────────────────────────┘            └───────────────────────────┘
```

- Claude Code starts the server on demand. It keeps **one persistent SSH connection**
  to the login node and reconnects automatically if the connection drops.
- Only lightweight commands run on the login node. Real work always goes through
  `sbatch`.
- Nothing needs to be installed on the cluster. The server only needs the standard tools
  found on any Linux login node (`ls`, `stat`, `grep`, `find`, `realpath`) plus the
  SLURM client commands.

## What Claude can do

| Area | Tools |
|---|---|
| Cluster docs | `hpc_list_docs`, `hpc_read_doc`, `hpc_search_docs`: your site notes plus a built-in SLURM primer |
| Files (read) | `hpc_list_directory`, `hpc_get_file_info`, `hpc_read_file` (≤1 MB, else head/tail preview), `hpc_head_file` (decompresses `.gz`), `hpc_tail_file`, `hpc_search_in_file`, `hpc_find_files`, `hpc_storage_usage` |
| Files (write) | `hpc_write_file`, `hpc_edit_file`: only inside the writable paths you configure |
| SLURM (read) | `hpc_queue`, `hpc_cluster_info` (optionally per-node CPUs/RAM/GPUs), `hpc_job_details`, `hpc_job_efficiency`, `hpc_job_history`, `hpc_list_reservations` |
| SLURM (act) | `hpc_submit_job`, `hpc_cancel_job`, `hpc_hold_job`, `hpc_release_job`: only your own jobs |
| Script generation | `hpc_generate_sbatch` (checks partition, time, CPU, memory, GPU and QoS limits from your profile), `hpc_generate_rmd`, `hpc_generate_rmd_render_job` |
| Projects | `hpc_resolve_project`: short aliases for project directories |
| Workspaces | `hpc_list_workspaces`, `hpc_workspace_info`, `hpc_allocate_workspace`, `hpc_extend_workspace`, `hpc_release_workspace`. Only on clusters that run [hpc-workspace](https://github.com/holgerBerger/hpc-workspace), enabled in the profile |

## Safety model

The server lets Claude act on a shared machine with **your** account, so the guardrails
are built into the tools rather than left to the model's judgement:

- **There is no general shell tool.** Every tool runs one fixed command. All arguments are
  shell-quoted, and job IDs, sizes, names and `#SBATCH` values are validated.
- **No delete, move, copy or chmod.** Those tools simply don't exist.
- **Path policy:**
  - Writes are allowed only under `[paths].write`.
  - Reads are allowed only under `write` + `read`.
  - `[paths].forbidden` always wins.
  - `.ssh`, `.gnupg`, `.netrc`, `.aws`, `.kube` and `.docker` directories are blocked
    everywhere.
  - Paths are checked again **after the server resolves symlinks**, so a symlink inside
    scratch can't be used to reach `/etc` or another user's data.
- **Large and binary files** never enter the conversation. Claude gets a preview or
  metadata instead.
- **Only your own jobs** can be cancelled, held or released.
- **Mutating tools** are marked as such for the client. Claude Code asks you before every
  call that is not on your allow-list (see [step 6](#6-optional-skip-prompts-for-read-only-tools)).
- **Credentials stay local.** The SSH key and `known_hosts` are mounted read-only into the
  container and are never baked into the image. The host key is verified by default.

## Setup

You need:

- An account on the cluster with SSH access (key-based).
- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview).
- **Either** [Docker](https://docs.docker.com/get-docker/) (Docker Desktop on macOS/Windows)
  **or** [uv](https://docs.astral.sh/uv/getting-started/installation/) if you'd rather not
  use Docker.

On Windows, run everything inside WSL2.

### 1. SSH access to the cluster

The server logs in with an SSH key, the same way you do. If you can already run
`ssh you@login.cluster.org` without typing a password, skip to the last bullet.

```bash
# Create a key (press Enter to accept the default path; a passphrase is optional)
ssh-keygen -t ed25519 -C "you@laptop"

# Install it on the cluster (asks for your cluster password one last time)
ssh-copy-id -i ~/.ssh/id_ed25519.pub you@login.cluster.org

# Test it. This also records the server's host key in ~/.ssh/known_hosts,
# which slurm-mcp uses to verify that it's talking to the real server.
ssh you@login.cluster.org hostname
```

- **The login node is only reachable through a gateway?** Test with
  `ssh -J you@gateway.org you@login-node`, then fill in `HPC_JUMP_HOST` in `.env` (step 2).
  Connect to the gateway directly once as well, so its host key is recorded too.
- **The login node is only reachable over the institute VPN?** Connect the VPN first.
  Docker Desktop routes container traffic through your laptop's network, VPN included.
- **You use a host alias from `~/.ssh/config`** (e.g. `ssh mycluster`)? slurm-mcp does not
  read `~/.ssh/config`. Use the real `HostName`, `User` and `Port` values in `.env`.

### 2. Get the code and create `.env`

```bash
git clone https://github.com/romelllo/slurm-mcp.git
cd slurm-mcp
cp .env.example .env
```

Edit `.env`. The minimum is:

```bash
HPC_HOST=login.cluster.org      # the login node
HPC_USER=you                    # your cluster username
HPC_KEY_PATH=~/.ssh/id_ed25519  # private key from step 1
```

See [all variables](#env) for the jump host, key passphrase, project aliases, and more.
`.env` is gitignored. Never commit it.

### 3. Describe your cluster

```bash
cp -r cluster.example cluster
```

`cluster/` is gitignored, so your site details stay private. Edit `cluster/cluster.toml`:

```toml
[cluster]
name = "My University HPC"
description = """
Login node is for editing/submitting only. Home has a 50 GB quota; keep data in /scratch/{user}.
"""

[paths]
write = ["/scratch/{user}/"]                 # where Claude may create/edit files
read  = ["/home/{user}/", "/shared/data/"]   # extra read-only locations
forbidden = ["/etc/"]

[slurm]
default_partition = "cpu"
qos = []                                     # allowed --qos values, if your site uses them
preamble = ["module load python/3.11"]       # added to every generated job script

[[slurm.partitions]]
name = "cpu"
max_time = "7-00:00:00"
max_cpus = 64
max_mem_gb = 500
max_gpus = 0
```

`{user}` is replaced with `HPC_USER`. Every limit is optional; leave it out if you don't
know it. The [reference](#clustertoml) lists all options.

Next, put your cluster's rules into **`cluster/docs/*.md`**: storage and quotas, partitions
and QoS, the module system or conda, who to contact, and so on. Copying the relevant parts
of your cluster's wiki works well. Claude reads these through `hpc_read_doc` and
`hpc_search_docs`. Short, factual bullet points work best.

> **Tip:** the most important rules (e.g. "never compute on the login node", quotas) belong
> in `[cluster].description` too. Claude sees that text at the start of every session.

### 4. Build and test

**With Docker:**

```bash
docker compose build
docker compose run --rm -T slurm-mcp --check
```

**Without Docker:**

```bash
uv sync
uv run slurm-mcp --check
```

`--check` prints the effective configuration, logs in, and checks that your paths exist:

```
Cluster profile : /app/cluster/cluster.toml
Cluster name    : My University HPC
SSH target      : you@login.cluster.org:22
Host key check  : on (/run/secrets/known_hosts)
Writable paths  : /scratch/you/
...
Connecting ... OK — logged in to login01, slurm 23.11.4
  ✓ /scratch/you/ (writable)
  ✓ /home/you/

All good.
```

If anything fails, see [Troubleshooting](#troubleshooting).

### 5. Connect Claude Code

Register the server once. Use the **absolute** path to your clone:

```bash
# With Docker
claude mcp add --scope user hpc -- \
  docker compose -f /ABSOLUTE/PATH/TO/slurm-mcp/docker-compose.yml run --rm -T slurm-mcp

# Without Docker
claude mcp add --scope user hpc -- \
  uv --directory /ABSOLUTE/PATH/TO/slurm-mcp run slurm-mcp
```

- `--scope user` makes the cluster available in every project.
- Use `--scope project` to register it only for the current repository (this writes
  `.mcp.json`, which you can commit for your team, since it contains no secrets).
- The server name **`hpc`** becomes the tool prefix: `mcp__hpc__hpc_queue`, etc.

Start Claude Code and run `/mcp`. `hpc` should show as **connected** with about 27 tools.
Each Claude Code session starts its own short-lived container, which exits when the
session ends.

### 6. Optional: skip prompts for read-only tools

By default Claude Code asks before **every** tool call. To let Claude browse files and the
queue freely while still asking before anything that changes the cluster, allow the
read-only tools in `~/.claude/settings.json` (all projects) or `.claude/settings.json` (one
project). The same block is in
[`examples/claude-settings.json`](examples/claude-settings.json):

```json
{
  "permissions": {
    "allow": [
      "mcp__hpc__hpc_list_docs",
      "mcp__hpc__hpc_read_doc",
      "mcp__hpc__hpc_search_docs",
      "mcp__hpc__hpc_list_directory",
      "mcp__hpc__hpc_get_file_info",
      "mcp__hpc__hpc_read_file",
      "mcp__hpc__hpc_head_file",
      "mcp__hpc__hpc_tail_file",
      "mcp__hpc__hpc_search_in_file",
      "mcp__hpc__hpc_find_files",
      "mcp__hpc__hpc_storage_usage",
      "mcp__hpc__hpc_resolve_project",
      "mcp__hpc__hpc_queue",
      "mcp__hpc__hpc_cluster_info",
      "mcp__hpc__hpc_job_details",
      "mcp__hpc__hpc_job_efficiency",
      "mcp__hpc__hpc_job_history",
      "mcp__hpc__hpc_list_reservations",
      "mcp__hpc__hpc_generate_sbatch",
      "mcp__hpc__hpc_generate_rmd",
      "mcp__hpc__hpc_generate_rmd_render_job"
    ]
  }
}
```

Writing files, submitting, cancelling, holding and releasing jobs, and workspace changes
will still ask every time.

## Using it

Just talk about the cluster. Some prompts that work well:

- *"What partitions are there and how busy are they? Which GPU nodes are free?"*
- *"Show me what's in /scratch/me/project and summarise the sample sheet."*
- *"Peek at the first reads of sample1_R1.fastq.gz."*
- *"Write an sbatch script that aligns all samples with bwa-mem2 as a job array, 16 cores
  each, then submit it."*
- *"Job 123456 failed. Find out why and fix the script."*
- *"How efficient were my jobs this week? Am I requesting too much memory?"*
- *"Which of my jobs from the last 3 days hit TIMEOUT or OUT_OF_MEMORY?"*
- *"Create an R Markdown QC notebook for these count tables and a job that renders it."*

It helps to add a line to your analysis project's `CLAUDE.md`:

```markdown
Data and compute for this project live on the HPC cluster. Use the `hpc` MCP tools for
anything on the cluster (never local shell/ssh). Project directory: /scratch/me/project.
```

## Configuration reference

### `.env`

| Variable | Default | Description |
|---|---|---|
| `HPC_HOST` | — | Login node hostname or IP (**required**) |
| `HPC_USER` | — | Cluster username (**required**) |
| `HPC_PORT` | `22` | SSH port |
| `HPC_KEY_PATH` | — | Private key on your machine. With Docker it's mounted at `/run/secrets/hpc_key` |
| `HPC_KEY_PASSPHRASE` | — | Key passphrase, if the key has one |
| `HPC_PASSWORD` | — | Only for servers that demand a password/second prompt *in addition to* the key |
| `HPC_JUMP_HOST` | — | Gateway host, like `ssh -J` |
| `HPC_JUMP_USER` | `HPC_USER` | Username on the gateway |
| `HPC_JUMP_PORT` | `22` | Gateway SSH port |
| `HPC_KNOWN_HOSTS` | `~/.ssh/known_hosts` | File with the cluster's host key(s) |
| `HPC_VERIFY_HOST_KEY` | `true` | `false` disables host-key verification (not recommended) |
| `HPC_CLUSTER_CONFIG` | `cluster/cluster.toml` | Cluster profile path (relative to the repo root) |
| `HPC_DOCS_DIR` | `docs/` next to the profile | Override the docs directory |
| `HPC_PROJECTS` | — | `alias:path` pairs, comma-separated. Relative paths are resolved under the first writable path, e.g. `rna:projects/rnaseq,ref:/shared/ref/hg38` |
| `HPC_DEFAULT_PROJECT` | — | Alias shown as the default project |
| `HPC_COMMAND_TIMEOUT` | `30` | Seconds before a remote command is abandoned |

### `cluster.toml`

| Key | Description |
|---|---|
| `[cluster] name` | Display name |
| `[cluster] description` | Short paragraph Claude always sees. Put the key rules here. `{user}` is expanded |
| `[cluster] docs_dir` | Docs folder relative to the profile (default `docs`) |
| `[cluster] login_shell` | `true` runs commands via `bash -lc`. Use it if `sbatch`/`squeue` are only on `PATH` in login shells |
| `[paths] write` | Directories Claude may create/edit files in. Globs allowed per component: `/ws/{user}-*` |
| `[paths] read` | Additional read-only directories |
| `[paths] forbidden` | Always denied, even inside the above |
| `[slurm] default_partition` | Used when the request doesn't name one (default: first partition) |
| `[slurm] qos` | Allowed `--qos` values (empty = not validated) |
| `[slurm] account` | Default `--account` |
| `[slurm] preamble` | Lines inserted into every generated job script (module loads, conda activation, …) |
| `[slurm.limits_without_qos] max_cpus / max_mem_gb / max_gpus` | Per-user limits without a QoS. Exceeding them gives a warning that suggests a QoS |
| `[[slurm.partitions]] name, description, max_time, max_cpus, max_mem_gb, max_gpus` | One block per partition. Limits are optional. `max_gpus = 0` means CPU-only |
| `[workspaces] enabled, max_days` | Turn on the hpc-workspace (`ws_*`) tools |
| `[rmd] setup` | Lines run before `Rscript` in R Markdown render jobs |

## Other MCP clients

Any MCP client that can launch a stdio server works.

**Claude Desktop:** add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hpc": {
      "command": "docker",
      "args": ["compose", "-f", "/ABSOLUTE/PATH/TO/slurm-mcp/docker-compose.yml",
               "run", "--rm", "-T", "slurm-mcp"]
    }
  }
}
```

On macOS, Claude Desktop may not see Docker on its `PATH`. If so, use the full path
`/usr/local/bin/docker`.

## Troubleshooting

Run `--check` first (step 4). It pinpoints most problems.

| Symptom | Fix |
|---|---|
| `known_hosts file not found` | SSH to the cluster once with plain `ssh` (step 1), or point `HPC_KNOWN_HOSTS` at the right file |
| `Host key ... could not be verified` | `known_hosts` has no entry for exactly `HPC_HOST` (and `HPC_JUMP_HOST`). Run `ssh <HPC_HOST>` once using the same name/IP as in `.env`. If the cluster was reinstalled, remove the stale entry with `ssh-keygen -R <host>` and reconnect |
| `SSH authentication failed` | Check `HPC_USER` and `HPC_KEY_PATH`. Does `ssh -i <key> user@host` work? If `ssh -v` shows `Partial success`, the server also wants a password: set `HPC_PASSWORD` |
| `Connect call failed` / timeout | Login node not reachable: connect the VPN, or configure `HPC_JUMP_HOST` |
| Docker: `bind source path does not exist` | The key or `known_hosts` path in `.env` is wrong, or `cluster/` wasn't created (`cp -r cluster.example cluster`) |
| Docker on Linux: permission denied reading the key | The container runs as UID 1000. Add `user: "${UID}:${GID}"` to the service in `docker-compose.yml` and export `UID`/`GID` (or run without Docker) |
| `sbatch: command not found` | SLURM is only on `PATH` in login shells. Set `login_shell = true` in `cluster.toml` |
| `/mcp` shows *failed* | Run the exact command from step 5 in a terminal. It should wait silently for input (Ctrl-C to quit). Any error printed there is the cause. Build the image first so startup is fast |
| Claude uses `ssh` in Bash instead of the tools | Add the `CLAUDE.md` line from [Using it](#using-it) |
| Job stays `PENDING` with `QOSMax…` | Over the per-user limit. Use a higher QoS or a smaller request (see your cluster docs) |

## Development

```bash
uv sync                                  # install with dev tools
uv run pytest                            # tests (no cluster needed — SSH is mocked)
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
uv run ty check src/
```

Code layout:

```
src/slurm_mcp/
├── server.py      tool registry, dispatcher, server instructions, --check
├── config.py      .env + cluster.toml → Settings
├── ssh.py         persistent asyncssh connection (jump host, host-key check, stdin upload)
├── paths.py       path policy + symlink resolution
├── builtin_docs/  generic SLURM primer served alongside the cluster docs
└── tools/         docs, filesystem, slurm, workspaces, scripts
```

To add a tool:

1. Write an `async` function in `tools/` that returns a `dict` and never raises.
2. Register it in `build_tools()` and `_HANDLERS` in `server.py`.
3. If it changes anything on the cluster, add it to `MUTATING_TOOLS`.

## License

[MIT](LICENSE)
