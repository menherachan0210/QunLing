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
QQBOT_APP_MEMORY=1280m
QQBOT_APP_MEMORY_SWAP=1792m
QQBOT_WORKER_MEMORY=512m
QQBOT_WORKER_MEMORY_SWAP=768m
```

For updater-managed installations, modify the authoritative active-compose
document, not a stale checkout. The updater preserves these installed parents
and memory caps. MemoryHigh begins reclaim at 1184/448 MiB, hard caps are
1280/512 MiB, and swap caps are 512/256 MiB. Sustained 30% memory pressure for
20 seconds lets systemd-oomd terminate only descendants of those two slices;
Docker's `unless-stopped` policy restores the affected service. QQ login,
the console, SSH and unrelated applications are outside the monitored slices.
Adjust budgets for the actual machine and workload before opting in.

The app cap is a ceiling, not preallocated RAM. Heavy-work admission also checks
the host's current `MemAvailable`, reserving 256 MiB plus active task estimates
before admitting another task, and checks remaining container capacity. This
allows short image-transfer peaks without removing the whole-host guard. These
estimates do not guarantee a host reserve against unrelated services growing.

When changing an existing installation, update both the Docker memory limits and
the parent slice; the smaller limit wins. Persist the slice unit and active-compose
document as well, or a reboot or update can restore the previous limits. Raising
limits can be applied live with `docker update` and `systemctl set-property`; do
not recreate a busy app just to increase its quota. Compose's `memswap_limit`
means RAM **plus** swap, so 1792 MiB with 1280 MiB RAM still permits only 512 MiB
of swap. Existing updater-managed installations retain their installed caps
until explicitly adjusted.

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
