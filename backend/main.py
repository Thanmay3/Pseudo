from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from database import get_db, get_engine
from models import BuildSession, Project, User
from auth import router as auth_router, current_user
from github_integration import router as github_router, visible_evidence


@asynccontextmanager
async def lifespan(app):
    engine = get_engine()
    try:
        with engine.connect() as connection:
            version = connection.scalar(text('SELECT version_num FROM alembic_version'))
            if version != '0003_github_evidence':
                raise RuntimeError('Run python -m alembic upgrade head before starting the backend.')
    except SQLAlchemyError:
        engine.dispose()
        raise RuntimeError('Database unavailable or migrations missing. Check DATABASE_URL and run python -m alembic upgrade head.') from None
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(title='Pseudo API', version='0.6.0', lifespan=lifespan)
app.include_router(auth_router)
app.include_router(github_router)


@app.middleware('http')
async def request_security(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origins = {value.strip() for value in os.getenv('TRUSTED_ORIGINS',
            'http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8000').split(',')}
        origin = request.headers.get('origin')
        if (request.headers.get('x-pseudo-request') != '1' or
            (origin is not None and origin not in origins) or
            request.headers.get('sec-fetch-site') == 'cross-site'):
            return JSONResponse(status_code=403, content={'detail': 'Request origin could not be verified'})
    try:
        response = await call_next(request)
    finally:
        if request.url.path == '/github/callback':
            # Uvicorn access logs must not retain the OAuth code or state.
            request.scope['query_string'] = b'' 
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.exception_handler(RequestValidationError)
async def invalid_input(request, error):
    # FastAPI's default validation errors can echo input, including passwords.
    return JSONResponse(status_code=422, content={'detail': 'Invalid or missing fields. Check the form requirements.'})


@app.exception_handler(SQLAlchemyError)
async def database_error(request, error):
    # Do not expose connection details, SQL parameters, or credentials.
    return JSONResponse(status_code=503, content={'detail': 'Database unavailable. Please try again.'})


class InputModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')


class ProjectInput(InputModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=1000)


class StartInput(InputModel):
    project_id: str = Field(min_length=1, max_length=36)
    task: str = Field(min_length=1, max_length=500)


class FinishInput(InputModel):
    accomplishment: str = Field(default='', max_length=2000)


class ProjectOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str
    created_at: datetime


class SessionOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    task: str
    start_time: datetime
    end_time: datetime | None
    duration_seconds: int | None
    accomplishment: str
    created_at: datetime
    is_published: bool


class PublicEvidenceOutput(BaseModel):
    kind: str
    reference: str
    title: str
    repository: str
    url: str


class PublicSessionOutput(BaseModel):
    # Deliberately separate from the private API's session/account models.
    evidence: list[PublicEvidenceOutput] = Field(default_factory=list)
    project_name: str
    task: str
    accomplishment: str
    end_time: datetime
    duration_seconds: int


class PublicProfileOutput(BaseModel):
    pseudonym: str
    total_projects: int
    total_sessions: int
    total_duration_seconds: int
    sessions: list[PublicSessionOutput]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@app.get('/health')
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text('SELECT 1'))
    return {'status': 'ok'}


@app.post('/projects', status_code=201, response_model=ProjectOutput)
def create_project(data: ProjectInput, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = Project(**data.model_dump(), user_id=user.id)
    db.add(project)
    db.commit()
    return project


@app.get('/projects', response_model=list[ProjectOutput])
def list_projects(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return db.scalars(select(Project).where(Project.user_id == user.id).order_by(Project.created_at, Project.id)).all()


@app.post('/sessions', status_code=201, response_model=SessionOutput)
def start_session(data: StartInput, db: Session = Depends(get_db), user: User = Depends(current_user)):
    # Serialize starts for this user, including starts on different projects.
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    if db.scalar(select(Project).where(Project.id == data.project_id, Project.user_id == user.id)) is None:
        raise HTTPException(404, 'Project not found')
    if db.scalar(select(BuildSession.id).join(Project).where(Project.user_id == user.id, BuildSession.end_time.is_(None))) is not None:
        raise HTTPException(409, 'Finish the active session before starting another')
    session = BuildSession(**data.model_dump(), start_time=utc_now())
    db.add(session)
    db.commit()
    return session


@app.get('/sessions/active')
def get_active_session(db: Session = Depends(get_db), user: User = Depends(current_user)):
    active = db.scalar(select(BuildSession).join(Project).where(Project.user_id == user.id, BuildSession.end_time.is_(None)))
    return {'session': SessionOutput.model_validate(active) if active else None, 'server_time': utc_now()}


@app.post('/sessions/{session_id}/finish', response_model=SessionOutput)
def finish_session(session_id: str, data: FinishInput, db: Session = Depends(get_db), user: User = Depends(current_user)):
    # Lock this row until commit, so simultaneous finishes cannot overwrite it.
    session = db.scalar(select(BuildSession).join(Project).where(
        BuildSession.id == session_id, Project.user_id == user.id).with_for_update(of=BuildSession))
    if session is None:
        raise HTTPException(404, 'Session not found')
    if session.end_time is not None:
        # Preserve Stage 2's idempotent retry: return the existing result, HTTP 200.
        return session
    session.end_time = max(utc_now(), session.start_time)
    session.duration_seconds = int((session.end_time - session.start_time).total_seconds())
    session.accomplishment = data.accomplishment
    db.commit()
    return session


@app.get('/sessions')
def list_sessions(db: Session = Depends(get_db), user: User = Depends(current_user)):
    completed = db.scalars(select(BuildSession).join(Project).where(Project.user_id == user.id, BuildSession.end_time.is_not(None)).order_by(
        BuildSession.end_time.desc(), BuildSession.id.desc(),
    )).all()
    return {
        'sessions': [SessionOutput.model_validate(session) for session in completed],
        'total_sessions': len(completed),
        'total_duration_seconds': sum(session.duration_seconds for session in completed),
    }


def set_publication(session_id: str, published: bool, db: Session, user: User):
    session = db.scalar(select(BuildSession).join(Project).where(
        BuildSession.id == session_id, Project.user_id == user.id).with_for_update(of=BuildSession))
    if session is None:
        raise HTTPException(404, 'Session not found')
    if session.end_time is None:
        raise HTTPException(409, 'Finish the session before publishing it')
    session.is_published = published
    db.commit()
    return session


@app.post('/sessions/{session_id}/publish', response_model=SessionOutput)
def publish_session(session_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return set_publication(session_id, True, db, user)


@app.post('/sessions/{session_id}/unpublish', response_model=SessionOutput)
def unpublish_session(session_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return set_publication(session_id, False, db, user)


@app.get('/profiles/{pseudonym}', response_model=PublicProfileOutput)
def public_profile(pseudonym: str, db: Session = Depends(get_db)):
    user = db.execute(select(User.id, User.pseudonym).where(func.lower(User.pseudonym) == pseudonym.lower())).one_or_none()
    if user is None:
        raise HTTPException(404, 'Profile not found')
    # Fetch only published completed work. Derive totals from this same snapshot.
    rows = db.execute(select(
        Project.id.label('project_id'), Project.name.label('project_name'),
        BuildSession.task, BuildSession.accomplishment, BuildSession.end_time,
        BuildSession.duration_seconds, BuildSession.id.label('session_id'),
    ).join(BuildSession, BuildSession.project_id == Project.id).where(
        Project.user_id == user.id, BuildSession.is_published.is_(True),
        BuildSession.end_time.is_not(None),
    ).order_by(BuildSession.end_time.desc(), BuildSession.id.desc())).mappings().all()
    evidence = visible_evidence(db, [row['session_id'] for row in rows])
    return PublicProfileOutput(
        pseudonym=user.pseudonym,
        total_projects=len({row['project_id'] for row in rows}),
        total_sessions=len(rows),
        total_duration_seconds=sum(row['duration_seconds'] for row in rows),
        sessions=[PublicSessionOutput(**{key: row[key] for key in PublicSessionOutput.model_fields if key != 'evidence'}, evidence=evidence.get(row['session_id'], [])) for row in rows],
    )
