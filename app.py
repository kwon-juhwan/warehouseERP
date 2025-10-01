# =============================
# app.py  (NiceGUI Frontend)
# =============================
# - NiceGUI 기반 간단 프론트엔드
# - BACKEND_URL 환경변수로 FastAPI 백엔드 주소를 설정
# - Railway 배포 시 $PORT 로 서버 오픈
#
# 주요 기능
# 1) 부자재 목록 조회/검색/저재고 필터
# 2) 부자재 등록 (code, name, current_m, min_threshold_m, reorder_qty_m)
# 3) 소모/입고 처리 (version 기반 동시성 체크 옵션 포함)
# 4) 실시간 알림(WebSocket) 토스트 표시
#
# 백엔드는 이전에 제공한 FastAPI(main.py) 기준

import os
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from nicegui import ui, app

BACKEND_URL = os.getenv('BACKEND_URL', 'http://127.0.0.1:8000')  # 예: https://your-backend.up.railway.app
PORT = int(os.getenv('PORT', '8080'))

# ----------------------
# HTTP 클라이언트 (async)
# ----------------------
@asynccontextmanager
def get_client():
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=20.0) as client:
        yield client

# ----------------------
# 유틸
# ----------------------

def ws_url_from_http(base_http: str) -> str:
    # http -> ws, https -> wss
    if base_http.startswith('https://'):
        return 'wss://' + base_http[len('https://'):]
    if base_http.startswith('http://'):
        return 'ws://' + base_http[len('http://'):]
    # 도메인만 온 경우 가정
    if base_http.startswith('wss://') or base_http.startswith('ws://'):
        return base_http
    return 'ws://' + base_http

async def toast_ok(msg: str):
    ui.notify(msg, type='positive', position='top-right', close_button='닫기')

async def toast_err(msg: str):
    ui.notify(msg, type='negative', position='top-right', close_button='닫기')

# ----------------------
# 상태
# ----------------------
materials = []  # 테이블 데이터 캐시

# ----------------------
# 컴포넌트: 헤더/툴바
# ----------------------
with ui.header().classes('items-center justify-between'):
    ui.label('📦 물류 부자재 관리 (NiceGUI Frontend)').classes('text-xl font-semibold')
    with ui.row().classes('items-center gap-2'):
        search_inp = ui.input(placeholder='이름/코드 검색').props('clearable').classes('w-64')
        low_only_chk = ui.checkbox('저재고만', value=False)
        ui.button('새로고침', on_click=lambda: load_materials()).props('flat color=primary')
        ui.button('부자재 등록', on_click=lambda: dlg_create.open()).props('color=primary')

# ----------------------
# 테이블
# ----------------------
cols = [
    {'name': 'id', 'label': 'ID', 'field': 'id', 'align': 'left', 'sortable': True},
    {'name': 'code', 'label': '코드', 'field': 'code', 'align': 'left', 'sortable': True},
    {'name': 'name', 'label': '이름', 'field': 'name', 'align': 'left', 'sortable': True},
    {'name': 'current_m', 'label': '현재고(m)', 'field': 'current_m', 'align': 'right', 'sortable': True},
    {'name': 'min_threshold_m', 'label': '임계치(m)', 'field': 'min_threshold_m', 'align': 'right'},
    {'name': 'reorder_qty_m', 'label': '권장발주량(m)', 'field': 'reorder_qty_m', 'align': 'right'},
    {'name': 'version', 'label': '버전', 'field': 'version', 'align': 'right'},
    {'name': 'actions', 'label': '작업', 'field': 'actions', 'align': 'left'},
]

material_table = ui.table(columns=cols, rows=[], row_key='id', pagination={'rowsPerPage': 10}).classes('w-full')

# 셀 렌더링: actions
@material_table.add_slot('body-cell-actions')
def _(row):
    with ui.row().classes('gap-2'):
        ui.button('소모', on_click=lambda r=row: open_consume_dialog(r)).props('size=sm outline color=negative')
        ui.button('입고', on_click=lambda r=row: open_replenish_dialog(r)).props('size=sm outline color=positive')

# 현재고가 임계치 이하인 행 강조
@material_table.add_slot('body-cell-current_m')
def _(row):
    cur = float(row['current_m']) if row['current_m'] is not None else 0.0
    th = float(row['min_threshold_m']) if row['min_threshold_m'] is not None else -1
    style = 'font-weight:600;' if th >= 0 and cur <= th else ''
    ui.html(f'<div style="text-align:right;{style}">{row["current_m"]}</div>')

# ----------------------
# 다이얼로그: 부자재 등록
# ----------------------
with ui.dialog() as dlg_create, ui.card().classes('min-w-[420px]'):
    ui.label('부자재 등록').classes('text-lg font-semibold')
    in_code = ui.input('코드').classes('w-full')
    in_name = ui.input('이름').classes('w-full')
    in_current = ui.number('현재고(m)', value=0, format='%.3f').classes('w-full')
    in_min = ui.number('임계치(m)', value=0, format='%.3f').classes('w-full')
    in_reorder = ui.number('권장 발주량(m)', value=0, format='%.3f').classes('w-full')
    with ui.row().classes('justify-end gap-2 mt-2'):
        ui.button('취소', on_click=dlg_create.close).props('flat')
        async def do_create():
            payload = {
                'code': in_code.value,
                'name': in_name.value,
                'current_m': str(in_current.value or 0),
                'min_threshold_m': str(in_min.value or 0),
                'reorder_qty_m': str(in_reorder.value or 0),
            }
            try:
                async with get_client() as c:
                    r = await c.post('/materials', json=payload)
                if r.status_code == 200:
                    await toast_ok('등록 완료')
                    dlg_create.close()
                    await load_materials()
                else:
                    await toast_err(f'등록 실패: {r.status_code} {r.text}')
            except Exception as e:
                await toast_err(f'오류: {e}')
        ui.button('등록', on_click=do_create).props('color=primary')

# ----------------------
# 다이얼로그: 소모/입고 공통
# ----------------------
async def do_movement(row: dict, amount: float, reason: str, mode: str):
    # mode: 'consume' | 'replenish'
    try:
        payload = {
            'amount_m': str(amount),
            'reason': reason,
            'expected_version': row.get('version', None)  # 동시성 체크 (선택)
        }
        async with get_client() as c:
            r = await c.post(f"/materials/{row['id']}/{mode}", json=payload)
        if r.status_code == 200:
            await toast_ok('처리 완료')
            await load_materials()
        else:
            await toast_err(f'실패: {r.status_code} {r.text}')
    except Exception as e:
        await toast_err(f'오류: {e}')

# 소모 다이얼로그

def open_consume_dialog(row: dict):
    with ui.dialog() as dlg, ui.card().classes('min-w-[380px]'):
        ui.label(f"소모: [{row['code']}] {row['name']}").classes('text-lg font-semibold')
        amt = ui.number('소모량(m)', value=0.0, format='%.3f').classes('w-full')
        reason = ui.input('사유(선택)').classes('w-full')
        with ui.row().classes('justify-end gap-2 mt-2'):
            ui.button('취소', on_click=dlg.close).props('flat')
            ui.button('소모', on_click=lambda: (asyncio.create_task(do_movement(row, amt.value or 0, reason.value or '', 'consume')), dlg.close())).props('color=negative')
    dlg.open()

# 입고 다이얼로그

def open_replenish_dialog(row: dict):
    with ui.dialog() as dlg, ui.card().classes('min-w-[380px]'):
        ui.label(f"입고: [{row['code']}] {row['name']}").classes('text-lg font-semibold')
        amt = ui.number('입고량(m)', value=0.0, format='%.3f').classes('w-full')
        reason = ui.input('사유(선택)').classes('w-full')
        with ui.row().classes('justify-end gap-2 mt-2'):
            ui.button('취소', on_click=dlg.close).props('flat')
            ui.button('입고', on_click=lambda: (asyncio.create_task(do_movement(row, amt.value or 0, reason.value or '', 'replenish')), dlg.close())).props('color=positive')
    dlg.open()

# ----------------------
# 데이터 로딩
# ----------------------
async def load_materials():
    q = search_inp.value or ''
    low_only = low_only_chk.value or False
    params = {}
    if q:
        params['q'] = q
    if low_only:
        params['low_only'] = 'true'
    try:
        async with get_client() as c:
            r = await c.get('/materials', params=params)
        if r.status_code == 200:
            rows = r.json()
            # actions 필드 채우기(placeholder)
            for row in rows:
                row['actions'] = ''
            material_table.rows = rows
            material_table.update()
        else:
            await toast_err(f'조회 실패: {r.status_code} {r.text}')
    except Exception as e:
        await toast_err(f'오류: {e}')

# ----------------------
# 실시간 알림(WebSocket): 브라우저 내 JS로 직접 연결
# ----------------------
ws_base = ws_url_from_http(BACKEND_URL)
alerts_ws_url = f"{ws_base}/ws/alerts"

async def setup_ws():
    # JS에서 WebSocket 연결 (wss 지원)
    js = f'''
    if (!window._alertsWS) {{
        const url = '{alerts_ws_url}';
        const ws = new WebSocket(url);
        window._alertsWS = ws;
        ws.onopen = () => console.log('WS connected:', url);
        ws.onclose = () => console.log('WS closed');
        ws.onmessage = (ev) => {{
            try {{
                const data = JSON.parse(ev.data);
                if (data && data.type === 'alert') {{
                    window.postMessage({{ kind: 'nicegui_alert', payload: data }}, '*');
                }}
            }} catch(e) {{ console.error(e); }}
        }}
    }}
    '''
    await ui.run_javascript(js)

# JS -> Python 브리지: window.postMessage 수신
ui.on('nicegui_alert', lambda e: ui.notify(f"🔔 {e.args['message']}", type='warning', position='top-right'))

# 브라우저에서 window.postMessage를 NiceGUI로 전달하도록 스니펫 설치
ui.add_head_html('''
<script>
window.addEventListener('message', (ev) => {
  if (ev && ev.data && ev.data.kind === 'nicegui_alert') {
    // NiceGUI로 이벤트 전달
    if (window.nicegui && window.nicegui.emit) {
      window.nicegui.emit('nicegui_alert', ev.data.payload);
    }
  }
});
</script>
''')

# 초기 로드
async def init_page():
    await load_materials()
    await setup_ws()

ui.timer(0.2, init_page, once=True)

# 푸터
with ui.footer().classes('justify-between'):
    ui.label('© Logistics Submaterials Frontend')
    ui.link('API 문서(백엔드)', BACKEND_URL + '/docs', new_tab=True)

# 앱 실행
PORT = int(os.getenv('PORT', '8080'))
ui.run(host='0.0.0.0', port=PORT)

# =============================
# requirements.txt
# =============================
# nicegui는 fastapi/starlette 내장
# httpx는 비동기 API 호출용
# Python 3.10+
nicegui>=2.0.0
httpx>=0.27.0

# =============================
# Dockerfile  (Railway 배포용)
# =============================
# syntax=docker/dockerfile:1
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Railway가 PORT 환경변수를 주입
ENV PORT=8080
CMD ["python", "app.py"]

if __name__ == "__main__":
    PORT = int(os.getenv('PORT', '8080'))
    ui.run(host='0.0.0.0', port=PORT)


# =============================
# .env.example
# =============================
# 로컬에서 프론트 실행 시 백엔드 주소
# BACKEND_URL=http://127.0.0.1:8000

# =============================
# 배포/실행 가이드
# =============================
# 1) 로컬 실행
#    set BACKEND_URL=http://127.0.0.1:8000
#    python app.py
#    -> http://127.0.0.1:8080 접속
#
# 2) Railway 배포
#    - 새 프로젝트 > 서비스 생성 > "Deploy from GitHub" 선택
#    - 이 리포( app.py / requirements.txt / Dockerfile 포함 ) 연결
#    - Deploy
#    - Variables 에 BACKEND_URL 추가(예: https://<your-backend>.up.railway.app)
#    - 배포가 완료되면 Railway가 제공하는 도메인으로 접속
#
# 3) 백엔드 CORS
#    - FastAPI에 CORS 미들웨어가 이미 allow_origins=["*"] 로 설정되어 있으면 교차 도메인 호출/WS가 동작합니다.
#
# 4) 실시간 알림
#    - 프론트는 BACKEND_URL 기준 /ws/alerts 로 웹소켓 연결합니다(https -> wss 변환).
#
# 5) 커스터마이징 포인트
#    - 테이블 컬럼 추가/정렬/필터, 페이지네이션 사이즈
#    - 소모/입고 시 expected_version 사용을 토글하려면 payload에서 제거 가능
#    - 알림 배지/목록 페이지 추가, /alerts REST 사용하여 읽음 처리 UI 구현 등
