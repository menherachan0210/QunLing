"""Admin routes must coexist with the memory-reset runtime patch on every run."""
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('console_patch_installer', ROOT / 'scripts/Apply-RuntimePatches.py')
patcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = patcher
SPEC.loader.exec_module(patcher)


def test_console_and_reset_patches_preflight_together_and_remain_idempotent(tmp_path):
    fixtures = {
        'src/langbot/pkg/api/http/controller/groups/platform/bots.py': '''        async def _(bot_uuid: str, request_context: RequestContext) -> str:
            return self.success(data=await self.ap.bot_service.list_event_route_statuses(request_context, bot_uuid))

        async def _dry_run_event_route(bot_uuid: str, request_context: RequestContext) -> str:
            json_data = await quart.request.json
            if not isinstance(json_data, dict):
                return self.http_status(400, -1, 'invalid request')

        # Other upstream routes are outside the patch contexts.
        async def inbound_test():
            try:
                result = await send_test()
            except ValueError as exc:
                return self.http_status(400, -1, str(exc))
            return self.success(data=result)

        @self.route(
            '/<bot_uuid>/admins',
            methods=['GET'],
        )
        async def admins():
            pass
''',
        'src/langbot/pkg/platform/adapters/aiocqhttp/adapter.py': '''        handler = PLATFORM_API_MAP.get(action)
        if handler is None:
            raise NotSupportedError(f'call_platform_api:{action}')
        return await handler(self.bot, params)

    def register_listener(
        self,
    ):
        pass
''',
    }
    for name, content in fixtures.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    names = ['host-group-memory-reset.patch', 'host-admin-console.patch']
    sources = [(ROOT / 'patches' / name).read_text(encoding='utf-8') for name in names]
    plans = [patcher.build_plan(name, tmp_path, source, 'host') for name, source in zip(names, sources)]
    assert all(plan.state == 'ready' for plan in plans)
    patcher.execute_plans(plans, apply=True)
    installed = {name: (tmp_path / name).read_bytes() for name in fixtures}
    second = [patcher.build_plan(name, tmp_path, source, 'host') for name, source in zip(names, sources)]
    assert all(plan.state == 'already-applied' for plan in second)
    patcher.execute_plans(second, apply=True)
    assert installed == {name: (tmp_path / name).read_bytes() for name in fixtures}
