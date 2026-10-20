"""Download a public release in bounded ranges and verify its SHA-256."""

import argparse
import concurrent.futures
import hashlib
from pathlib import Path
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('output', type=Path)
    parser.add_argument('size', type=int)
    parser.add_argument('sha256')
    args = parser.parse_args()
    chunk_size = 1024 * 1024
    parts = args.output.with_suffix(args.output.suffix + '.parts')
    parts.mkdir(parents=True, exist_ok=True)

    def fetch(start):
        end = min(start + chunk_size, args.size) - 1
        part = parts / str(start)
        if part.exists() and part.stat().st_size == end - start + 1:
            return part
        for attempt in range(4):
            try:
                separator = '&' if '?' in args.url else '?'
                url = f'{args.url}{separator}download_chunk={start}'
                request = urllib.request.Request(url, headers={
                    'Range': f'bytes={start}-{end}', 'User-Agent': 'LocalDeployment/1.0',
                })
                with urllib.request.urlopen(request, timeout=30) as response:
                    expected = f'bytes {start}-{end}/{args.size}'
                    if response.status != 206 or response.headers.get('Content-Range') != expected:
                        raise RuntimeError('Server did not return the requested byte range')
                    data = response.read(end - start + 2)
                if len(data) != end - start + 1:
                    raise RuntimeError('Incomplete download range')
                part.write_bytes(data)
                return part
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(attempt + 1)

    starts = list(range(0, args.size, chunk_size))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for index, _ in enumerate(pool.map(fetch, starts), 1):
            if index % 5 == 0 or index == len(starts):
                print(f'Downloaded {index}/{len(starts)} chunks', flush=True)
    temporary = args.output.with_suffix(args.output.suffix + '.verified-part')
    digest = hashlib.sha256()
    with temporary.open('wb') as output:
        for start in starts:
            data = (parts / str(start)).read_bytes()
            digest.update(data)
            output.write(data)
    if digest.hexdigest().lower() != args.sha256.lower():
        raise RuntimeError('Release SHA-256 mismatch; output was not installed')
    temporary.replace(args.output)
    print(f'Verified SHA-256: {digest.hexdigest()}', flush=True)


if __name__ == '__main__':
    main()
