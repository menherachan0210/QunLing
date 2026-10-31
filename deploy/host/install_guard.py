"""Install the optional Ubuntu/systemd 257+ host guard; never restart containers."""
from pathlib import Path
import argparse
import os
import subprocess


def run(*args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def install():
    if os.geteuid() != 0:
        raise RuntimeError('Run on the host as root')
    version = int(run('/usr/lib/systemd/systemd', '--version').split()[1])
    if version < 257 or not Path('/sys/fs/cgroup/cgroup.controllers').exists():
        raise RuntimeError('Requires systemd 257+ and cgroups v2')
    if run('docker', 'info', '--format', '{{.CgroupDriver}}') != 'systemd':
        raise RuntimeError('Docker must use its systemd cgroup driver')
    for binary in ('/usr/lib/systemd/systemd-oomd', '/usr/bin/atop'):
        if not Path(binary).is_file():
            raise RuntimeError('Install matching systemd-oomd and atop packages first')
    # Ubuntu enables whole-host swap killing by default. This deployment only
    # opts in its two bot slices; existing administrator overrides are preserved.
    base = Path('/etc/systemd/system')
    for relative in ('-.slice.d/10-oomd-root-slice-defaults.conf',
                     'user@.service.d/10-oomd-user-service-defaults.conf'):
        target = base / relative
        if target.is_symlink() and os.readlink(target) == '/dev/null':
            continue
        if target.exists() or target.is_symlink():
            raise RuntimeError('Review existing host OOM policy: ' + str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to('/dev/null')
    for name in ('qunling-bot.slice', 'qunling-files.slice'):
        (base / name).write_text(Path(__file__).with_name(name).read_text(), encoding='utf-8')
    Path('/etc/default/atop').write_text(
        'LOGOPTS=""\nLOGINTERVAL=15\nLOGGENERATIONS=2\nLOGPATH=/var/log/atop\n')
    drop = base / 'atop.service.d/qunling.conf'
    drop.parent.mkdir(parents=True, exist_ok=True)
    drop.write_text('[Service]\nUMask=0077\nMemoryMax=64M\nRestart=on-failure\nRestartSec=30s\n')
    run('systemctl', 'daemon-reload')
    for unit in ('-.slice', 'system.slice', 'user.slice', 'user@1000.service'):
        values = run('systemctl', 'show', '-p', 'ManagedOOMSwap', '-p',
                     'ManagedOOMMemoryPressure', '--', unit)
        if '=kill' in values:
            raise RuntimeError('Unexpected broad OOM policy: ' + unit)
    run('systemctl', 'unmask', '--runtime', 'systemd-oomd.service', 'systemd-oomd.socket', 'atop.service')
    run('systemd-analyze', 'verify', str(base / 'qunling-bot.slice'), str(base / 'qunling-files.slice'))
    run('systemctl', 'enable', '--now', 'systemd-oomd.service', 'atop.service',
        'atop-rotate.timer', 'qunling-bot.slice', 'qunling-files.slice')
    # Process accounting is unnecessary for resident-memory history.
    run('systemctl', 'disable', '--now', 'atopacct.service')
    print(run('oomctl'))
    print('Guard ready. Recreate app/file-worker with the matching cgroup_parent to activate containment.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', required=True)
    parser.parse_args()
    install()
