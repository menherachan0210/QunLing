"""Make one image-generation request to the configured sub2api provider."""

import argparse
import base64
import io
import json

import requests
from PIL import Image

from local_api import LocalAPI, STATE

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--edit', action='store_true', help='Edit the previous test PNG instead of generating a new image')
args = parser.parse_args()
api = LocalAPI()
state = json.loads((STATE / 'setup-state.json').read_text(encoding='utf-8'))
provider = api.call('GET', '/api/v1/provider/providers/' + state['provider_uuid'] + '?include_secret=true')['provider']
session = requests.Session()
session.trust_env = False
request_data = {
    'model': 'gpt-image-2.5-sunburst',
    'prompt': 'A simple flat illustration of a small orange cat wearing a blue space helmet, white background, no text.',
    'n': 1,
    'size': '1024x1024',
    'quality': 'auto',
    'response_format': 'b64_json',
}
operation = 'edits' if args.edit else 'generations'
if args.edit:
    request_data['prompt'] = 'Change only the cat space helmet from blue to green. Keep the orange cat, illustration style and white background unchanged.'
    request_data['images'] = [{'image_url': 'data:image/png;base64,' + base64.b64encode((STATE / 'image-generation-test.png').read_bytes()).decode()}]
print('Testing image ' + operation + ' with one request; this can take several minutes.', flush=True)
with session.post(
    provider['base_url'].rstrip('/') + '/images/' + operation,
    headers={'Authorization': 'Bearer ' + provider['api_keys'][0]},
    json=request_data, timeout=(15, 300), stream=True,
) as response:
    print('Image endpoint HTTP status:', response.status_code, flush=True)
    content = bytearray()
    for chunk in response.iter_content(65536):
        content.extend(chunk)
        if len(content) > 40 * 1024 * 1024:
            raise RuntimeError('Image endpoint response exceeded 40 MiB')
    result = json.loads(content)
    if not response.ok:
        error = str(result.get('error', result)).replace(provider['api_keys'][0], '[redacted]')
        raise RuntimeError(error[:500])
data = result.get('data', [])
if not data or not data[0].get('b64_json'):
    raise RuntimeError('The endpoint did not return b64_json image data')
payload = base64.b64decode(data[0]['b64_json'], validate=True)
picture = Image.open(io.BytesIO(payload))
picture.verify()
extension = {'JPEG': 'jpg', 'WEBP': 'webp'}.get(picture.format, 'png')
output_name = 'image-edit-test' if args.edit else 'image-generation-test'
output = STATE / (output_name + '.' + extension)
output.write_bytes(payload)
report = {
    'model': request_data['model'], 'status': 'passed', 'width': picture.width,
    'height': picture.height, 'format': picture.format, 'bytes': len(payload), 'file': output.name,
}
(STATE / ('image-edit-api-result.json' if args.edit else 'image-generation-api-result.json')).write_text(json.dumps(report, indent=2), encoding='utf-8')
print('Image ' + operation + ' passed:', picture.format, picture.size, len(payload), 'bytes', flush=True)
