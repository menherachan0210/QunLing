"""Test natural-language generation and follow-up editing through local chat."""

import asyncio
import base64
import io
import json
import time

from PIL import Image
from websockets.asyncio.client import connect

from local_api import LocalAPI, STATE


async def main():
    api = LocalAPI()
    state = json.loads((STATE / 'setup-state.json').read_text(encoding='utf-8'))
    available = api.call('GET', '/api/v1/tools?pipeline_uuid=' + state['pipeline_uuid'])['tools']
    if not {'generate_image', 'edit_image'} <= {tool.get('name') for tool in available}:
        raise RuntimeError('Install and configure the image plugin before running this paid check')
    pipeline = api.call('GET', '/api/v1/pipelines/' + state['pipeline_uuid'])['pipeline']
    url = 'ws://127.0.0.1:5300/api/v1/pipelines/' + state['pipeline_uuid'] + '/ws/connect?session_type=person'
    report = {}
    async with connect(url, origin='http://127.0.0.1:5300', proxy=None, max_size=32 * 1024 * 1024) as socket:
        await socket.send(json.dumps({
            'type': 'authenticate', 'token': api.http.headers['Authorization'][7:],
            'workspace_uuid': pipeline['workspace_uuid'],
        }))
        connected = json.loads(await socket.recv())
        if connected.get('type') != 'connected':
            raise RuntimeError('Local debug chat authentication failed')

        async def ask(label, prompt):
            print('Testing ' + label + ' through the local agent...', flush=True)
            await socket.send(json.dumps({'type': 'message', 'stream': True, 'message': [{'type': 'Plain', 'text': prompt}]}))
            outputs = []
            text = ''
            seen_response = False
            started = time.monotonic()
            deadline = started + 660
            while time.monotonic() < deadline:
                try:
                    frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=1))
                except TimeoutError:
                    if seen_response:
                        active = api.call('GET', '/api/v1/monitoring/executions?botId=websocket-proxy-bot&status=running&status=queued&limit=100')
                        if not any(connected['connection_id'] in str(item) for item in active['items']):
                            break
                    continue
                if frame.get('type') == 'error':
                    raise RuntimeError(str(frame.get('message')))
                if frame.get('type') not in ('response', 'stream_chunk'):
                    continue
                seen_response = True
                data = frame.get('data', {})
                text = data.get('content', '') or text
                for component in data.get('message_chain', []):
                    if component.get('type') != 'Image' or not component.get('base64'):
                        continue
                    encoded = component['base64'].split(',', 1)[-1]
                    pixels = base64.b64decode(encoded, validate=True)
                    picture = Image.open(io.BytesIO(pixels))
                    picture.verify()
                    path = STATE / (label + '-tool-result.png')
                    path.write_bytes(pixels)
                    outputs.append({'file': path.name, 'format': picture.format, 'size': picture.size, 'bytes': len(pixels)})
                    print(label + ': received valid ' + str(picture.format) + ' image', flush=True)
            else:
                raise RuntimeError(label + ' did not finish within 660 seconds')
            executions = api.call('GET', '/api/v1/monitoring/executions?botId=websocket-proxy-bot&limit=50')['items']
            execution = next((item for item in executions if (
                item.get('session_id') == 'person_websocket_' + connected['connection_id']
                and item.get('input_preview') == prompt
            )), None)
            calls = []
            if execution:
                detail = api.call('GET', '/api/v1/monitoring/executions/' + execution['source'] + '/' + execution['id'] + '?section=tool_calls')
                calls = [call['tool_name'] for call in detail.get('tool_calls', [])]
            image_calls = [name for name in calls if name in ('generate_image', 'edit_image')]
            expected = 'generate_image' if label == 'generation' else 'edit_image'
            passed = len(outputs) == 1 and image_calls == [expected]
            report[label] = {'images': outputs, 'text': text, 'image_tool_calls': image_calls,
                             'elapsed_seconds': round(time.monotonic() - started, 1), 'passed': passed}
            (STATE / 'picture-tools-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            if not outputs:
                raise RuntimeError(label + ' returned no image: ' + text)
            if not passed:
                raise RuntimeError(label + ' must return exactly one image from one ' + expected + ' call')

        await ask('generation', '请生成一张白色背景、中央只有一个紫色正方形的极简图片，不要文字。')
        await ask('editing', '把刚才图片里的紫色正方形改成橙色，其余保持不变。')
    print('Natural-language generation and follow-up editing both returned valid images.', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
