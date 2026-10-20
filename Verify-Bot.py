"""Exercise local debug chat without sending any messages to QQ."""

import asyncio
import base64
import io
import json
import uuid

from PIL import Image, ImageDraw
from websockets.asyncio.client import connect

from local_api import LocalAPI, STATE


async def main():
    api = LocalAPI()
    state = json.loads((STATE / 'setup-state.json').read_text(encoding='utf-8'))
    pipeline = api.call('GET', '/api/v1/pipelines/' + state['pipeline_uuid'])['pipeline']
    report = {}
    url = 'ws://127.0.0.1:5300/api/v1/pipelines/' + state['pipeline_uuid'] + '/ws/connect?session_type=person'
    async with connect(url, proxy=None, origin='http://127.0.0.1:5300', max_size=4 * 1024 * 1024) as socket:
        await socket.send(json.dumps({
            'type': 'authenticate', 'token': api.http.headers['Authorization'].removeprefix('Bearer '),
            'workspace_uuid': pipeline['workspace_uuid'],
        }))
        connected = json.loads(await socket.recv())
        if connected.get('type') != 'connected':
            raise RuntimeError('Local debug chat authentication failed')

        async def ask(text, image=None):
            chain = [{'type': 'Plain', 'text': text}]
            if image:
                chain.append({'type': 'Image', 'base64': 'data:image/png;base64,' + image})
            await socket.send(json.dumps({'type': 'message', 'stream': True, 'message': chain}))
            async with asyncio.timeout(240):
                while True:
                    frame = json.loads(await socket.recv())
                    if frame.get('type') == 'error':
                        raise RuntimeError(str(frame.get('message', 'Debug chat error')))
                    if frame.get('type') in ('response', 'stream_chunk'):
                        data = frame.get('data', {})
                        if data.get('is_final'):
                            # The final UI chunk can precede server-side run completion.
                            # Wait before submitting the next turn so it isn't steering.
                            for _ in range(60):
                                await asyncio.sleep(0.5)
                                active = api.call('GET', '/api/v1/monitoring/executions?botId=websocket-proxy-bot&status=running&status=queued&limit=100')
                                if not any(connected['connection_id'] in str(item) for item in active['items']):
                                    break
                            else:
                                raise RuntimeError('Debug run did not become idle')
                            return data.get('content', '')

        # Synthetic test image contains no user files or personal information.
        picture = Image.new('RGB', (400, 220), 'white')
        drawing = ImageDraw.Draw(picture)
        drawing.ellipse((30, 30, 190, 190), fill='red')
        drawing.rectangle((240, 50, 360, 170), fill='blue')
        buffer = io.BytesIO()
        picture.save(buffer, format='PNG')
        report['vision'] = await ask('描述图片中左边和右边各是什么形状、什么颜色。', base64.b64encode(buffer.getvalue()).decode())
        print('Vision:', report['vision'], flush=True)
        marker = '银杏-' + uuid.uuid4().hex[:8]
        report['memory_marker'] = marker
        report['remember'] = await ask('这是部署验收测试，请使用长期记忆工具保存：测试项目代号为“' + marker + '”。这不是个人资料。')
        print('Remember:', report['remember'], flush=True)
        api.call('POST', '/api/v1/monitoring/sessions/reset-context', {
            'bot_id': 'websocket-proxy-bot', 'session_id': 'person_websocket_' + connected['connection_id'],
        })
        report['recall_after_reset'] = await ask('请从长期记忆中查找部署验收的测试项目代号，只回复代号；不知道就说不知道。')
        report['memory_passed'] = marker in report['recall_after_reset']
        print('Recall after context reset:', report['recall_after_reset'], flush=True)
        report['cleanup'] = await ask('验收结束，请用记忆工具删除刚才保存的测试项目代号“' + marker + '”。')
        print('Cleanup:', report['cleanup'], flush=True)
        remaining = api.call('POST', '/api/v1/plugins/langbot-team/LongTermMemory/page-api', {
            'page_id': 'memory_console', 'endpoint': '/episodes/list', 'method': 'POST',
            'body': {
                'collection_id': state['memory_uuid'],
                'user_key': 'websocket-proxy-bot:person_websocket_' + connected['connection_id'],
                'page': 1, 'page_size': 50, 'statuses': ['active', 'superseded', 'archived', 'deleted'],
            },
        })
        report['cleanup_verified'] = remaining['total'] == 0 and not remaining['episodes']
        print('Cleanup verified against storage:', report['cleanup_verified'], flush=True)
        (STATE / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if not report['memory_passed']:
            raise RuntimeError('Long-term memory recall did not return the test marker')
        if not report['cleanup_verified']:
            raise RuntimeError('The test memory remains in storage after the forget tool call')


if __name__ == '__main__':
    asyncio.run(main())
