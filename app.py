import os
import time
from typing import Optional, Dict, Any, List

import pandas as pd
import requests
import streamlit as st

# ---------- 설정 ----------
BACKEND_URL = os.getenv("BACKEND_URL", "").rstrip("/")
if not BACKEND_URL:
    st.warning("환경변수 BACKEND_URL이 비어있습니다. Railway Variables에 백엔드 URL을 넣어주세요.")
st.set_page_config(page_title="Warehouse ERP - Submaterials", layout="wide")


# ---------- HTTP 유틸 ----------
class Api:
    def __init__(self, base: str):
        self.base = base

    def get(self, path: str, **kw):
        return requests.get(self.base + path, timeout=20, **kw)

    def post(self, path: str, **kw):
        return requests.post(self.base + path, timeout=20, **kw)

    def patch(self, path: str, **kw):
        return requests.patch(self.base + path, timeout=20, **kw)

api = Api(BACKEND_URL) if BACKEND_URL else None


def toast(msg: str, ok: bool = True):
    (st.success if ok else st.error)(msg, icon="✅" if ok else "⚠️")


# ---------- 데이터 로딩 ----------
@st.cache_data(ttl=10)
def fetch_materials(q: str = "", low_only: bool = False) -> pd.DataFrame:
    params = {}
    if q:
        params["q"] = q
    if low_only:
        params["low_only"] = "true"
    r = api.get("/materials", params=params)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # 숫자 컬럼 정리
    for col in ["current_m", "min_threshold_m", "reorder_qty_m"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


@st.cache_data(ttl=10)
def fetch_alerts(only_unread: bool = False) -> pd.DataFrame:
    params = {"only_unread": "true"} if only_unread else {}
    r = api.get("/alerts", params=params)
    r.raise_for_status()
    rows = r.json()
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def create_material(payload: Dict[str, Any]) -> Dict[str, Any]:
    r = api.post("/materials", json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"등록 실패: {r.status_code} {r.text}")
    return r.json()


def move_stock(material_id: int, amount_m: float, reason: str, mode: str, expected_version: Optional[int]) -> Dict[str, Any]:
    payload = {"amount_m": str(amount_m), "reason": reason}
    if expected_version is not None:
        payload["expected_version"] = expected_version
    r = api.post(f"/materials/{material_id}/{mode}", json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"{mode} 실패: {r.status_code} {r.text}")
    return r.json()


def mark_alert_read(alert_id: int):
    r = api.post(f"/alerts/{alert_id}/read")
    if r.status_code != 200:
        raise RuntimeError(f"읽음 처리 실패: {r.status_code} {r.text}")


# ---------- 상단 바 ----------
st.title("📦 물류 부자재 관리 (Streamlit)")
with st.sidebar:
    st.subheader("필터 / 새로고침")
    q = st.text_input("검색(이름/코드)", "")
    low_only = st.checkbox("저재고만 보기", value=False)
    auto_refresh_sec = st.number_input("자동 새로고침(초)", min_value=0, max_value=120, value=0,
                                       help="0이면 자동 새로고침 없음")
    if st.button("새로고침"):
        st.cache_data.clear()

    st.divider()
    st.subheader("백엔드")
    st.caption("현재 BACKEND_URL")
    st.code(BACKEND_URL or "(미설정)", language="text")

if auto_refresh_sec > 0:
    st.experimental_rerun  # type: ignore
    st.experimental_set_query_params(ts=str(int(time.time())))  # query 변경으로 캐시 키 변화
    st.experimental_singleton.clear()  # noop 보호
    st.experimental_memo.clear()  # legacy 보호
    st.cache_data.clear()
    st.experimental_rerun()  # 즉시 갱신


# ---------- 탭 ----------
tab_list = st.tabs(["재고", "등록", "알림"])
tab_stock, tab_create, tab_alerts = tab_list

if not api:
    st.stop()

# ===== 재고 탭 =====
with tab_stock:
    try:
        df = fetch_materials(q, low_only)
    except Exception as e:
        st.error(f"재고 조회 실패: {e}")
        df = pd.DataFrame()

    col_left, col_mid, col_right = st.columns([2, 2, 1])

    with col_left:
        st.subheader("재고 목록")
        st.dataframe(df, use_container_width=True, height=500)

        if not df.empty:
            csv = df.to_csv(index=False).encode("utf-8-sig")
            st.download_button("CSV 다운로드", data=csv, file_name="materials.csv", mime="text/csv")

    with col_mid:
        st.subheader("소모/입고 처리")
        if df.empty:
            st.info("부자재가 없습니다. 먼저 등록하세요.")
        else:
            row_labels = [f"[{r.code}] {r.name}" for r in df.itertuples()]
            idx = st.selectbox("대상 선택", options=list(range(len(df))), format_func=lambda i: row_labels[i])
            target = df.iloc[idx]

            st.write(f"현재고: **{target['current_m']} m** / 임계치: **{target['min_threshold_m']} m** / 버전: {target['version']}")
            mode = st.radio("작업", ["소모(출고)", "입고(보충)"], horizontal=True)
            amt = st.number_input("수량(m)", min_value=0.001, step=0.1, format="%.3f")
            reason = st.text_input("사유(선택)", "")

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("실행", type="primary", use_container_width=True, disabled=amt <= 0):
                    try:
                        updated = move_stock(
                            int(target["id"]),
                            amount_m=amt,
                            reason=reason,
                            mode="consume" if mode.startswith("소모") else "replenish",
                            expected_version=int(target["version"]) if not pd.isna(target["version"]) else None,
                        )
                        toast("처리 완료 ✅")
                        st.cache_data.clear()
                        st.experimental_rerun()
                    except Exception as e:
                        toast(str(e), ok=False)

    with col_right:
        st.subheader("저재고 빠른 보기")
        try:
            low_df = fetch_materials("", True)
            st.dataframe(low_df, use_container_width=True, height=300)
        except Exception as e:
            st.error(f"저재고 조회 실패: {e}")

# ===== 등록 탭 =====
with tab_create:
    st.subheader("부자재 등록")
    with st.form("create_form", clear_on_submit=False):
        code = st.text_input("코드", "")
        name = st.text_input("이름", "")
        current_m = st.number_input("현재고(m)", min_value=0.0, step=1.0, format="%.3f", value=0.0)
        min_threshold_m = st.number_input("임계치(m)", min_value=0.0, step=1.0, format="%.3f", value=0.0)
        reorder_qty_m = st.number_input("권장 발주량(m)", min_value=0.0, step=1.0, format="%.3f", value=0.0)
        submitted = st.form_submit_button("등록", type="primary")
        if submitted:
            try:
                payload = {
                    "code": code.strip(),
                    "name": name.strip(),
                    "current_m": str(current_m),
                    "min_threshold_m": str(min_threshold_m),
                    "reorder_qty_m": str(reorder_qty_m),
                }
                if not payload["code"] or not payload["name"]:
                    raise RuntimeError("코드/이름은 필수입니다.")
                create_material(payload)
                toast("등록 완료 ✅")
                st.cache_data.clear()
            except Exception as e:
                toast(str(e), ok=False)

# ===== 알림 탭 =====
with tab_alerts:
    st.subheader("알림")
    unread_only = st.checkbox("읽지 않은 알림만 보기", value=False)
    try:
        adf = fetch_alerts(unread_only)
        st.dataframe(adf, use_container_width=True, height=500)
    except Exception as e:
        st.error(f"알림 조회 실패: {e}")
        adf = pd.DataFrame()

    if not adf.empty:
        ids = adf["id"].tolist()
        sel = st.selectbox("읽음 처리 대상", options=ids)
        if st.button("읽음 처리"):
            try:
                mark_alert_read(int(sel))
                toast("읽음 처리 완료 ✅")
                st.cache_data.clear()
                st.experimental_rerun()
            except Exception as e:
                toast(str(e), ok=False)

st.caption("※ Streamlit은 WebSocket 대신 주기적 폴링으로 알림을 갱신합니다.")
