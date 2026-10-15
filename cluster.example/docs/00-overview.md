# Example University HPC — overview

Replace this file with your own cluster's rules. Anything written here is available to
the assistant through `hpc_list_docs`, `hpc_read_doc` and `hpc_search_docs`.

Good things to document:

- **Login / access** — which node is the login node, VPN or jump-host requirements.
- **Storage** — home vs scratch vs project space, quotas, purge policies, backups.
- **Partitions and QoS** — time limits, default and maximum CPUs/memory/GPUs, how to get more.
- **Software** — module system (`module avail`), conda, containers (Apptainer/Singularity).
- **Etiquette** — what not to do on the login node, fair-share rules, who to contact.

Short, factual bullet points work best. Split large topics into several files
(`01-storage.md`, `02-partitions.md`, …).
