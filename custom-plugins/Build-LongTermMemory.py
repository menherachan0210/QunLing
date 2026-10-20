"""Build the local LongTermMemory package without caches or credentials."""

import hashlib
from pathlib import Path
import zipfile

import yaml


root = Path(__file__).resolve().parent
source = root / 'LongTermMemory'
manifest = yaml.safe_load((source / 'manifest.yaml').read_text(encoding='utf-8'))
version = manifest['metadata']['version']
package = root / f'LongTermMemory-{version}.lbpkg'
excluded = {'__pycache__', '.pytest_cache', '.ruff_cache', '.git', '.venv', '.env'}
with zipfile.ZipFile(package, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if path.is_file() and not excluded.intersection(relative.parts) and path.suffix != '.pyc':
            archive.write(path, relative.as_posix())
print(f'Built {package.name}')
print(f'SHA256: {hashlib.sha256(package.read_bytes()).hexdigest()}')
