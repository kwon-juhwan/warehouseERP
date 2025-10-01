from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Field, Session, create_engine, select
from pydantic import BaseModel, condecimal
import decimal
import asyncio
import uuid
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======== DB 설정 ========
DATABASE_URL = "sqlite:///./submaterials.db"  # 운영은 postgresql+psycopg://user:pwd@host/db 권장
engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})

# ======== 모델 ========
class Material(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True, description="부자재 코드 (예: FAB-001)")
    name: str
    current_m: decimal.Decimal = Field(default=decimal.Decimal("0"), description="현재고(m)")
    min_threshold_m: decimal.Decimal = Field(default=decimal.Decimal("0"), description="임계치(m)")
    reorder_qty_m: decimal.Decimal = Field(default=decimal.Decimal("0"), description="권장 발주량(m)")
    unit: str = Field(default="m")
    is_active: bool = Field(default=True)
    version: int = Field(default=1, description="낙관적 잠금용 버전")

class MaterialLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    material_id: int = Field(index=True, foreign_key="material.id")
    change_m: decimal.Decimal  # +는 입고, -는 출고
    reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

class Alert(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    material_id: int = Field(index=True, foreign_key="material.id")
    level: str = Field(description="LOW|CRITICAL 등급")
    message: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_read: bool = Field(default=False)

def init_db():
    SQLModel.metadata.create_all(engine)

# ======== 스키마 ========
class MaterialCreate(BaseModel):
    code: str
    name: str
    current_m: condecimal(ge=0) = decimal.Decimal("0")
    min_threshold_m: condecimal(ge=0) = decimal.Decimal("0")
    reorder_qty_m: condecimal(ge=0) = decimal.Decimal("0")

class MaterialUpdate(BaseModel):
    name: Optional[str] = None
    min_threshold_m: Optional[condecimal(ge=0)] = None
    reorder_qty_m: Optional[condecimal(ge=0)] = None
    is_active: Optional[bool] = None

class Movement(BaseModel):
    amount_m: condecimal(gt=0)
    reason: Optional[str] = None
    expected_version: Optional[int] = None  # 낙관적 잠금: 프론트가 전달하면 불일치 시 409

# ======== 의존성 ========
def get_session():
    with Session(engine) as session:
        yield session

# ======== WebSocket 알림 허브 ========
class AlertHub:
    def __init__(self):
        self.active: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket):
        token = str(uuid.uuid4())
        await websocket.accept()
        self.active[token] = websocket
        return token

    def disconnect(self, token: str):
        self.active.pop(token, None)

    async def broadcast(self, payload: dict):
        to_drop = []
        for token, ws in self.active.items():
            try:
                await ws.send_json(payload)
            except Exception:
                to_drop.append(token)
        for t in to_drop:
            self.disconnect(t)

alert_hub = AlertHub()

async def push_alert(material: Material, level: str, session: Session):
    msg = f"[{material.code}] {material.name} 재고 {material.current_m}{material.unit} (임계치 {material.min_threshold_m}{material.unit})"
    alert = Alert(material_id=material.id, level=level, message=msg)
    session.add(alert)
    session.commit()
    session.refresh(alert)
    await alert_hub.broadcast({"type": "alert", "level": level, "message": msg, "material_id": material.id, "alert_id": alert.id})

# ======== 임계치 평가 ========
def evaluate_threshold(material: Material) -> Optional[str]:
    # 필요 시 임계치 0일 때는 무시
    if material.min_threshold_m is None:
        return None
    if material.current_m <= material.min_threshold_m:
        # 여유롭게 0 이하면 CRITICAL
        if material.current_m <= decimal.Decimal("0"):
            return "CRITICAL"
        return "LOW"
    return None

async def check_all_thresholds_and_alert(session: Session):
    materials = session.exec(select(Material).where(Material.is_active == True)).all()
    for m in materials:
        level = evaluate_threshold(m)
        if level:
            await push_alert(m, level, session)

# ======== FastAPI 앱 ========
app = FastAPI(title="Submaterials Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)


async def run_threshold_job():
    # 별도 세션 생성
    with Session(engine) as session:
        await check_all_thresholds_and_alert(session)

# ======== 라우트: Material ========
@app.post("/materials", response_model=Material)
def create_material(payload: MaterialCreate, session: Session = Depends(get_session)):
    if session.exec(select(Material).where(Material.code == payload.code)).first():
        raise HTTPException(status_code=409, detail="이미 존재하는 코드입니다.")
    m = Material(
        code=payload.code,
        name=payload.name,
        current_m=decimal.Decimal(payload.current_m),
        min_threshold_m=decimal.Decimal(payload.min_threshold_m),
        reorder_qty_m=decimal.Decimal(payload.reorder_qty_m),
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    return m

@app.get("/materials", response_model=List[Material])
def list_materials(session: Session = Depends(get_session), q: Optional[str] = None, low_only: bool = False):
    stmt = select(Material)
    if q:
        stmt = stmt.where((Material.name.contains(q)) | (Material.code.contains(q)))
    materials = session.exec(stmt).all()
    if low_only:
        materials = [m for m in materials if evaluate_threshold(m)]
    return materials

@app.get("/materials/{material_id}", response_model=Material)
def get_material(material_id: int, session: Session = Depends(get_session)):
    m = session.get(Material, material_id)
    if not m:
        raise HTTPException(status_code=404, detail="부자재를 찾을 수 없습니다.")
    return m

@app.patch("/materials/{material_id}", response_model=Material)
def update_material(material_id: int, payload: MaterialUpdate, session: Session = Depends(get_session)):
    m = session.get(Material, material_id)
    if not m:
        raise HTTPException(status_code=404, detail="부자재를 찾을 수 없습니다.")

    if payload.name is not None:
        m.name = payload.name
    if payload.min_threshold_m is not None:
        m.min_threshold_m = decimal.Decimal(payload.min_threshold_m)
    # 여기 수정: 월러스 연산자 제거 + 변수명 오타(reorder_tx_m)도 정정
    if payload.reorder_qty_m is not None:
        m.reorder_qty_m = decimal.Decimal(payload.reorder_qty_m)
    if payload.is_active is not None:
        m.is_active = payload.is_active

    m.version += 1
    session.add(m)
    session.commit()
    session.refresh(m)
    return m

# ======== 입출고 처리 ========
def apply_movement(material: Material, delta: decimal.Decimal, session: Session, reason: Optional[str]):
    # 재고 변경
    new_val = material.current_m + delta
    if new_val < 0:
        raise HTTPException(status_code=400, detail="재고가 음수가 될 수 없습니다.")
    material.current_m = new_val
    material.version += 1
    session.add(material)
    # 로그 적재
    log = MaterialLog(material_id=material.id, change_m=delta, reason=reason)
    session.add(log)
    session.commit()
    session.refresh(material)
    return material, log

@app.post("/materials/{material_id}/consume", response_model=Material)
async def consume(material_id: int, payload: Movement, session: Session = Depends(get_session)):
    m = session.get(Material, material_id)
    if not m:
        raise HTTPException(status_code=404, detail="부자재를 찾을 수 없습니다.")
    # 낙관적 잠금
    if payload.expected_version and payload.expected_version != m.version:
        raise HTTPException(status_code=409, detail=f"버전 불일치. 최신 버전은 {m.version} 입니다.")
    material, _ = apply_movement(m, -decimal.Decimal(payload.amount_m), session, payload.reason)
    # 임계치 평가 후 알림
    level = evaluate_threshold(material)
    if level:
        await push_alert(material, level, session)
    return material

@app.post("/materials/{material_id}/replenish", response_model=Material)
async def replenish(material_id: int, payload: Movement, session: Session = Depends(get_session)):
    m = session.get(Material, material_id)
    if not m:
        raise HTTPException(status_code=404, detail="부자재를 찾을 수 없습니다.")
    if payload.expected_version and payload.expected_version != m.version:
        raise HTTPException(status_code=409, detail=f"버전 불일치. 최신 버전은 {m.version} 입니다.")
    material, _ = apply_movement(m, decimal.Decimal(payload.amount_m), session, payload.reason)
    # 재고가 회복된 경우 별도 알림은 선택
    return material

# ======== 로그 & 알림 ========
@app.get("/materials/{material_id}/logs", response_model=List[MaterialLog])
def get_logs(material_id: int, session: Session = Depends(get_session)):
    if not session.get(Material, material_id):
        raise HTTPException(status_code=404, detail="부자재를 찾을 수 없습니다.")
    return session.exec(select(MaterialLog).where(MaterialLog.material_id == material_id).order_by(MaterialLog.created_at.desc())).all()

@app.get("/alerts", response_model=List[Alert])
def list_alerts(only_unread: bool = False, session: Session = Depends(get_session)):
    stmt = select(Alert).order_by(Alert.created_at.desc())
    alerts = session.exec(stmt).all()
    if only_unread:
        alerts = [a for a in alerts if not a.is_read]
    return alerts

@app.post("/alerts/{alert_id}/read")
def mark_alert_read(alert_id: int, session: Session = Depends(get_session)):
    a = session.get(Alert, alert_id)
    if not a:
        raise HTTPException(status_code=404, detail="알림을 찾을 수 없습니다.")
    a.is_read = True
    session.add(a)
    session.commit()
    return {"ok": True}

# ======== WebSocket: 실시간 알림 ========
@app.websocket("/ws/alerts")
async def alerts_ws(ws: WebSocket):
    token = await alert_hub.connect(ws)
    try:
        while True:
            # 클라이언트에서 ping 등을 보낼 수 있으므로 읽기 대기
            await ws.receive_text()
    except WebSocketDisconnect:
        alert_hub.disconnect(token)

from fastapi.responses import RedirectResponse, Response

# 홈으로 들어오면 문서로 리다이렉트
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")

# 파비콘 요청은 로그 지저분하니 204로 응답
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)
