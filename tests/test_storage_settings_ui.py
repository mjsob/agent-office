"""설정에서 직접 디스크를 줄이는 흐름과 실행 전 확인을 점검해요.
실행: python3 tests/test_storage_settings_ui.py [office.html 경로] [결과 폴더]
실제 디스크 작업은 가짜 응답으로 바꿔요.
"""
import json
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

html_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'payload/office.html'
output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
if output:
    output.mkdir(parents=True, exist_ok=True)
base = {'unchanged': True, 'rev': 1, 'update': {}}
html = html_path.read_text()
now = time.time()
results = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    try:
        for width, theme in [(320, 'light'), (390, 'dark'), (1500, 'light')]:
            state = {**base, 'unchanged': True, 'storage_notice_preferences': {'hidden': True, 'mode': 'never'}, 'storage_result_closed': ''}
            measured = {'status': 'ok', 'checked_at': now, 'drive': 'C:', 'free_gib': 36.1, 'reclaim_gib': 11.7}
            idle = {'available': True, 'can_start': True, 'status': 'idle'}
            state.update(storage_notice=measured.copy(), storage_compact=idle.copy())
            context = browser.new_context(viewport={'width': width, 'height': 920})
            writes, errors, dialogs = [], [], []
            accept = {'value': True}
            context.route('http://office.test/**', lambda r: r.fulfill(body=html, content_type='text/html'))
            context.route('**/api/state*', lambda r: r.fulfill(json=state))
            def post(route):
                data = route.request.post_data_json
                writes.append(data)
                if data['action'] == 'storage_compact':
                    assert data.get('manual') is True and data.get('confirmed') is True
                    state['storage_compact'] = {**idle, 'can_start': False, 'status': 'preparing'}
                    route.fulfill(json={'storage_compact': state['storage_compact']})
                elif data['action'] == 'storage_refresh':
                    state['storage_notice'] = {**measured, 'refreshing': True}
                    route.fulfill(json={'storage_notice': state['storage_notice']})
                else:
                    raise AssertionError(data)
            context.route('**/api', post)
            font = Path('/mnt/c/Windows/Fonts/malgun.ttf')
            if font.is_file():
                context.route('**/test-font.ttf', lambda r: r.fulfill(body=font.read_bytes(), content_type='font/ttf'))
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            def dialog(d):
                dialogs.append(d.message)
                d.accept() if accept['value'] else d.dismiss()
            page.on('dialog', dialog)
            page.goto('http://office.test/?theme=' + theme)
            page.wait_for_function('lastStorageNotice?.reclaim_gib === 11.7')
            if font.is_file():
                page.add_style_tag(content="@font-face{font-family:TestKorean;src:url('/test-font.ttf')}body,button{font-family:TestKorean,sans-serif}")
                page.evaluate('document.fonts.ready')
            page.locator('#settingsBtn').click()
            run = page.locator('#storageManualRun')
            assert run.is_enabled() and page.locator('#storageNotice').is_hidden()
            assert '11.7GiB' in page.locator('#storageSettingsMetrics').inner_text()
            assert page.locator('#storageSettingsChecked').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            accept['value'] = False
            run.click()
            assert not writes and dialogs and 'WSL' in dialogs[-1]
            accept['value'] = True
            run.click()
            page.wait_for_function('!storageStarting && lastStorageAction.status === "preparing"')
            assert len(writes) == 1 and run.is_disabled()
            def render(notice, action=idle):
                state.update(storage_notice=notice, storage_compact=action)
                page.evaluate('([n,a]) => renderStorageNotice(n,a,{hidden:true,mode:"never"})', [notice, action])
            render({**measured, 'reclaim_gib': 0})
            assert run.is_disabled() and '회수할 공간이 없어요' in page.locator('#storageSettingsMetrics').inner_text()
            render({'status': 'stale', 'checked_at': now - 7300})
            assert run.is_disabled() and page.locator('#storageRefresh').is_enabled()
            render(None)
            assert run.is_disabled() and page.locator('#storageRefresh').is_enabled()
            render({'status': 'unavailable'})
            assert run.is_disabled()
            render(measured, {**idle, 'available': False, 'can_start': False})
            assert run.is_disabled() and page.locator('#storageRefresh').is_disabled()
            assert '사용할 수 없어요' in page.locator('#storageSettingsMetrics').inner_text()
            render(measured, {**idle, 'status': 'running', 'can_start': False, 'phase': 'finalizing', 'elapsed_seconds': 125})
            assert run.is_disabled() and page.locator('#storageRefresh').is_disabled()
            assert '마무리' in page.locator('#storageSettingsAction').inner_text()
            render(measured)
            page.locator('#storageRefresh').click()
            page.wait_for_function('!storageRefreshing && lastStorageNotice.refreshing')
            assert writes[-1]['action'] == 'storage_refresh' and run.is_disabled()
            assert page.locator('#storageRefresh').is_disabled()
            render(measured)
            assert run.is_enabled()
            if output:
                page.locator('.storage-settings').screenshot(path=str(output / f'settings-{width}-{theme}.png'))
            assert not errors, errors
            results.append({'width': width, 'theme': theme, 'passed': True, 'cases': ['below-threshold-hidden-notice', 'confirmation-cancel', 'manual-contract', 'duplicate-disabled', 'zero', 'stale', 'missing-notice', 'unavailable', 'missing-tool', 'finalizing', 'remeasure', 'no-overflow'], 'errors': errors})
            context.close()
    finally:
        browser.close()
if output:
    (output / 'browser-result.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
print(json.dumps(results, ensure_ascii=False))
