"""공통 사용량 설정의 안내와 입력값, 작은 화면과 숨김 처리를 확인해요."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('html', nargs='?', type=Path, default=Path(__file__).resolve().parents[1] / 'payload/office.html')
parser.add_argument('--screenshots', type=Path)
args = parser.parse_args()
html = args.html.read_text()
resource = dict(supported=True, cpu_percent=70, memory_high_gb=0, logical_cpus=12,
                physical_memory_bytes=64*1024**3, memory_current_bytes=8*1024**3,
                cpu_usage_percent=43, applies_to='이 PC의 모든 Office 작업에 함께 적용해요.',
                uncontrolled='Windows 프로그램과 GPU는 제한하지 않아요.')
results = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    for width in (320, 390, 1400):
        for theme in ('light', 'dark'):
            page = browser.new_page(viewport=dict(width=width, height=1000))
            errors, writes = [], []
            page.on('pageerror', lambda e: errors.append(str(e)))
            state = dict(session='s', root='/repo', sessions=[], agents={}, jobs=[], tasks=[], msgs=[],
                         features=dict(mac=False, resources=True), update={}, resources=resource)
            page.route('http://office.test/**', lambda r: r.fulfill(body=html, content_type='text/html'))
            page.route('**/api/state*', lambda r: r.fulfill(json=state))
            def post(route):
                data = route.request.post_data_json
                writes.append(data)
                route.fulfill(json=dict(resources={**resource, **{k:data[k] for k in ('cpu_percent','memory_high_gb')}}))
            page.route('**/api', post)
            page.goto('http://office.test/?theme='+theme)
            page.wait_for_selector('#resbox b', state='attached')
            page.evaluate('clearInterval(refreshTimer)')
            page.locator('#settingsBtn').click()
            box = page.locator('#resbox')
            assert box.is_visible()
            text = box.inner_text()
            for expected in ('Office 전체 사용량 제한', 'Office 작업이 한도를 함께 써요', '모든 대화와 프로젝트', 'CPU 합계 최대', '메모리 합계 기준', '지금 CPU 합계 43%'):
                assert expected in text, (width, theme, expected, text)
            assert '지금 시작하는 작업부터' not in text
            assert page.locator('#resCpu').input_value() == '70'
            assert page.locator('#resMem').input_value() == '0'
            box.locator('summary').click()
            assert '논리 CPU 12개' in box.inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert box.evaluate('e=>e.scrollWidth<=e.clientWidth+1')
            page.locator('#resCpu').fill('30')
            page.locator('#resMem').fill('2')
            with page.expect_response('**/api'):
                page.locator('#resApply').click()
            page.wait_for_function('document.querySelector("#resCpu").value==="30"')
            assert writes == [dict(session='s', action='resource_limits', cpu_percent=30, memory_high_gb=2)], writes
            page.evaluate('document.activeElement.blur()')
            page.evaluate('r=>{lastResSig="";renderResources(r)}', {**resource, 'supported':False, 'reason':'사용량 제한을 적용하지 못했어요.'})
            assert box.locator('input').count() == 0
            assert '쓸 수 없어요' in box.inner_text()
            page.evaluate('renderResources(null)')
            assert box.is_hidden()
            page.evaluate('r=>{lastResSig="";renderResources(r)}', resource)
            if args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(args.screenshots/f'resources-{width}-{theme}.png'))
            assert not errors, errors
            results.append(dict(width=width, theme=theme, passed=True))
            page.close()
    browser.close()
print(json.dumps(results, ensure_ascii=False))
