# SLURM basics (generic)

Generic guidance that applies to any SLURM cluster. Site-specific rules in the
cluster's own docs take precedence over anything here.

## Golden rules

- The login node is shared. Never run heavy computation there — submit it with `sbatch`.
- Request what you need, not the maximum. Smaller requests start sooner.
- After a job finishes, check `hpc_job_efficiency` and adjust CPUs/memory for next time.
- Write job output to scratch/project storage, not to a small home quota.

## Resource requests

| Option | Meaning |
|---|---|
| `--cpus-per-task=N` | CPU cores for a single multi-threaded program |
| `--mem=20G` | Memory per node. Job is killed (OUT_OF_MEMORY) if exceeded |
| `--time=D-HH:MM:SS` | Wall-clock limit. Job is killed (TIMEOUT) if exceeded |
| `--partition=NAME` | Queue to run in |
| `--qos=NAME` | Quality of service; often unlocks higher limits |
| `--gres=gpu:1` / `--gres=gpu:a100:1` | GPUs (optionally a specific type) |
| `--nodelist=node01` | Pin to specific node(s) |
| `--array=1-100%10` | Job array: 100 tasks, at most 10 at once; task id in `$SLURM_ARRAY_TASK_ID` |
| `--mail-type=END,FAIL --mail-user=me@x` | E-mail notifications |

Use `$SLURM_CPUS_PER_TASK` inside scripts to pass the thread count to tools
(e.g. `samtools sort -@ $SLURM_CPUS_PER_TASK`).

## Job states

| State | Meaning |
|---|---|
| PENDING (PD) | Waiting. The REASON column says why (Resources, Priority, QOSMaxCpuPerUserLimit, …) |
| RUNNING (R) | Running |
| COMPLETED (CD) | Finished with exit code 0 |
| FAILED (F) | Non-zero exit code — read the `.err` log |
| TIMEOUT (TO) | Hit `--time`; request more time or checkpoint |
| OUT_OF_MEMORY (OOM) | Hit `--mem`; request more memory |
| CANCELLED (CA) | Cancelled by the user or an admin |

Common pending reasons:

- `Resources` / `Priority` — normal, just waiting.
- `QOSMaxCpuPerUserLimit`, `QOSMaxMemoryPerUser`, `AssocGrpCpuLimit` — over a per-user limit;
  wait for your other jobs or use a higher QoS if the cluster offers one.
- `ReqNodeNotAvail` — requested node is down/drained or reserved.
- `PartitionTimeLimit` — `--time` is longer than the partition allows; the job will never start.

## Useful commands

```bash
squeue -u $USER                    # my jobs
sinfo                              # partitions and node states
sinfo -N -o '%N %P %C %m %G %t'    # per-node CPUs (A/I/O/T), memory, GPUs, state
scontrol show job <id>             # full details of a queued/running job
sacct -j <id> --format=JobID,State,Elapsed,MaxRSS,ExitCode
seff <id>                          # efficiency summary (if installed)
scancel <id>                       # cancel
srun --pty --cpus-per-task=2 --mem=4G --time=1:00:00 bash   # interactive shell
```

## Debugging a failed job

1. `hpc_job_details` → State and ExitCode.
2. `hpc_tail_file` on the job's `.err` and `.out` logs.
3. OUT_OF_MEMORY → raise `--mem` (look at MaxRSS); TIMEOUT → raise `--time`.
4. "command not found" → the environment (module / conda) was not set up inside the job script.
