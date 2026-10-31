# Host pressure containment

Optional Ubuntu host setup for small VPS deployments using systemd 257+,
cgroups v2 and Docker's systemd cgroup driver. Review existing OOM policies
before installation. Install `systemd-oomd` at the same version as systemd
and `atop`; do not upgrade the operating system just to add these packages.

Run `sudo python3 deploy/host/install_guard.py --install` on the host, then
recreate **only app and file-worker** with these Compose environment variables:

```
QQBOT_APP_CGROUP_PARENT=qunling-bot.slice
QQBOT_WORKER_CGROUP_PARENT=qunling-files.slice
QQBOT_APP_MEMORY=1152m
QQBOT_APP_MEMORY_SWAP=1664m
QQBOT_WORKER_MEMORY=512m
QQBOT_WORKER_MEMORY_SWAP=768m
```

For updater-managed installations, modify the authoritative active-compose
document, not a stale checkout. The updater preserves these installed parents
and memory caps. MemoryHigh begins reclaim at 1056/448 MiB, hard caps are
1152/512 MiB, and swap caps are 512/256 MiB. Sustained 30% memory pressure for
20 seconds lets systemd-oomd terminate only descendants of those two slices;
Docker's `unless-stopped` policy restores the affected service. QQ login,
the console, SSH and unrelated applications are outside the monitored slices.
Adjust budgets for the actual machine and workload before opting in.

Verify `oomctl` lists only the two intended slices and `docker inspect` reports
the corresponding HostConfig.CgroupParent. Atop records process/resource
history every 15 seconds in `/var/log/atop`; daily rotation retains today and
recent days with `LOGGENERATIONS=2`. Logs use root-only permissions and never
need to be exposed through the console. Read a prior day's file with
`sudo atop -r /var/log/atop/atop_YYYYMMDD`.

This is recovery from sustained memory pressure, not proof that every timeout
is caused by memory. Inspect the request journal, provider errors and host
history before attributing a later failure.

References: [systemd-oomd](https://www.freedesktop.org/software/systemd/man/259/systemd-oomd.service.html),
[resource controls](https://www.freedesktop.org/software/systemd/man/259/systemd.resource-control.html).
