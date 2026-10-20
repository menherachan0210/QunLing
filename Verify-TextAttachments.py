"""Verify scoped TXT reading via local chat, without sending QQ messages."""

import asyncio
import base64
import json
import time
import uuid

from websockets.asyncio.client import connect

from local_api import LocalAPI, STATE


async def main():
    api = LocalAPI()
    state = json.loads((STATE / 'setup-state.json').read_text(encoding='utf-8'))
    tools = api.call('GET', '/api/v1/tools?pipeline_uuid=' + state['pipeline_uuid'])['tools']
    if 'read_text_attachment' not in {tool['name'] for tool in tools}:
        raise RuntimeError('Install the TXT reader before running this check')
    pipeline = api.call('GET', '/api/v1/pipelines/' + state['pipeline_uuid'])['pipeline']
    url = 'ws://127.0.0.1:5300/api/v1/pipelines/' + state['pipeline_uuid'] + '/ws/connect?session_type=person'
    report = {}
    async with connect(url, origin='http://127.0.0.1:5300', proxy=None, max_size=2 * 1024 * 1024) as socket:
        await socket.send(json.dumps({
            'type': 'authenticate', 'token': api.http.headers['Authorization'][7:],
            'workspace_uuid': pipeline['workspace_uuid'],
        }))
        connected = json.loads(await socket.recv())
        if connected.get('type') != 'connected':
            raise RuntimeError('Local chat authentication failed')

        async def ask(label, prompt, *, attachment=None, expected=None, original=None):
            chain = [{'type': 'Plain', 'text': prompt}]
            if attachment:
                chain.append(attachment)
            print('Testing ' + label + '...', flush=True)
            started = time.monotonic()
            await socket.send(json.dumps({'type': 'message', 'stream': True, 'message': chain}))
            response = ''
            seen_response = False
            while time.monotonic() - started < 180:
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
                if frame.get('type') in ('response', 'stream_chunk'):
                    seen_response = True
                    response = frame.get('data', {}).get('content') or response
            else:
                raise RuntimeError(label + ' did not finish within 180 seconds')

            recent = api.call('GET', '/api/v1/monitoring/executions?botId=websocket-proxy-bot&limit=50')['items']
            execution = next((item for item in recent if (
                item.get('session_id') == 'person_websocket_' + connected['connection_id']
                and item.get('input_preview', '').startswith(prompt)
            )), None)
            calls = []
            if execution:
                detail = api.call('GET', '/api/v1/monitoring/executions/' + execution['source'] + '/' + execution['id'] + '?section=tool_calls')
                calls = detail.get('tool_calls', [])
            reader_calls = [call for call in calls if call['tool_name'] == 'read_text_attachment']
            if expected is None:
                passed = not calls
            else:
                # Inspect only this synthetic fixture's tool output.
                results = []
                for call in reader_calls:
                    value = call.get('result', '')
                    if isinstance(value, str):
                        try:
                            value = json.loads(value)
                        except (ValueError, TypeError):
                            pass
                    results.append(str(value))
                tool_text = '\n'.join(results)
                passed = bool(reader_calls) and all(value in response for value in expected)
                passed = passed and (original is None or original in tool_text)
            report[label] = {
                'passed': passed, 'response': response,
                'tools': [call['tool_name'] for call in calls],
                'elapsed_seconds': round(time.monotonic() - started, 1),
            }
            (STATE / 'text-attachments-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            if not passed:
                raise RuntimeError(label + ' failed: ' + response)

        def attachment(name, content, encoding):
            payload = content.encode(encoding)
            return {
                'type': 'File', 'id': 'local-test-' + uuid.uuid4().hex, 'name': name,
                'size': len(payload), 'base64': base64.b64encode(payload).decode('ascii'),
            }

        marker = '白桦-' + uuid.uuid4().hex[:8]
        text = '项目代号：' + marker + '\n今天天汽很好,我们去公圆散步.\n'
        await ask('upload_only', '这是稍后要读的TXT附件。先不要读取或分析，只回复收到。',
                  attachment=attachment('verification-utf8.txt', text, 'utf-8-sig'))
        await ask('read_recent_utf8', '请读取我刚才上传的TXT，给出项目代号，并指出错别字和半角标点。',
                  expected=[marker, '天气', '公园'], original=marker)

        marker = '木槿-' + uuid.uuid4().hex[:8]
        text = '项目代号：' + marker + '\n这份文件使用中文编码，标点必须保留。\n'
        await ask('read_current_gb18030', '请读取这次附带的TXT，只回复里面的项目代号。',
                  attachment=attachment('verification-gb18030.txt', text, 'gb18030'),
                  expected=[marker], original='这份文件使用中文编码')

    print('Current and recent TXT reading passed, including UTF-8 and GB18030.', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
