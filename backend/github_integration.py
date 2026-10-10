"""GitHub OAuth and selected public evidence; GitHub remains the source of code."""
import os
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Literal
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import httpx
from authlib.integrations.httpx_client import OAuth2Client
from authlib.common.errors import AuthlibBaseError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import COOKIE, current_user, token_hash
from database import get_db
from models import User, Project, BuildSession, GitHubConnection, GitHubOAuthState, GitHubEvidence, utc_now

router = APIRouter(tags=['GitHub'])
API = 'https://api.github.com'
AUTHORIZE = 'https://github.com/login/oauth/authorize'
TOKEN = 'https://github.com/login/oauth/access_token'
HEADERS = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Pseudo-local'}


def settings():
    client_id, secret = os.getenv('GITHUB_CLIENT_ID'), os.getenv('GITHUB_CLIENT_SECRET')
    if not client_id or not secret:
        raise HTTPException(503, 'GitHub OAuth is not configured on the backend')
    return client_id, secret


def cipher():
    try:
        return Fernet(os.environ['GITHUB_TOKEN_KEY'].encode())
    except (KeyError, ValueError):
        raise HTTPException(503, 'GitHub credential encryption is not configured') from None


def decrypt(value):
    try:
        return cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        raise HTTPException(503, 'GitHub credential cannot be read; check the encryption key') from None


def oauth_client():
    client_id, secret = settings()
    return OAuth2Client(client_id, secret,
        redirect_uri=os.getenv('GITHUB_CALLBACK_URL', 'http://127.0.0.1:8000/github/callback'),
        token_endpoint_auth_method='client_secret_post', code_challenge_method='S256',
        timeout=6, headers={'Accept': 'application/json'}, follow_redirects=False)


def http_client():
    return httpx.Client(timeout=4, follow_redirects=False)


def api(path, token=None, params=None):
    # Paths are constructed here from validated IDs, never arbitrary client URLs.
    headers = dict(HEADERS)
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        with http_client() as client:
            response = client.get(API + path, headers=headers, params=params)
        if response.status_code == 401:
            raise HTTPException(409, 'GitHub authorization expired or was revoked. Disconnect and reconnect.')
        if response.status_code in (403, 429):
            raise HTTPException(503, 'GitHub is rate limiting or denying this request. Try again later.')
        if response.status_code == 404:
            raise HTTPException(404, 'Public GitHub resource not available')
        if response.status_code == 409:
            raise HTTPException(409, 'This repository has no commits yet')
        if response.status_code != 200:
            raise HTTPException(502, 'GitHub could not complete this request')
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'GitHub could not be reached. Try again later.') from None


def revoke(token):
    client_id, secret = settings()
    try:
        with http_client() as client:
            response = client.request('DELETE', f'{API}/applications/{client_id}/token',
                auth=(client_id, secret), json={'access_token': token}, headers=HEADERS)
        return response.status_code in (204, 404)
    except httpx.HTTPError:
        return False


def lock_user(db, user):
    db.execute(select(User.id).where(User.id == user.id).with_for_update())


def connection(db, user):
    row = db.get(GitHubConnection, user.id)
    if row is None:
        raise HTTPException(409, 'Connect GitHub first')
    if row.expires_at and row.expires_at <= utc_now():
        raise HTTPException(409, 'GitHub authorization expired. Disconnect and reconnect.')
    return row, decrypt(row.encrypted_token)


def owned_project(db, user, project_id):
    project = db.scalar(select(Project).where(Project.id == project_id, Project.user_id == user.id))
    if project is None:
        raise HTTPException(404, 'Project not found')
    return project


def owned_session(db, user, session_id):
    row = db.scalar(select(BuildSession).join(Project).where(BuildSession.id == session_id, Project.user_id == user.id))
    if row is None:
        raise HTTPException(404, 'Session not found')
    return row


def public_repo(repo_id, token):
    # Authentication provides quota, NEVER permission to publish private metadata.
    # Even a 200 response is rejected unless GitHub explicitly says public.
    repo = api(f'/repositories/{repo_id}', token=token)
    if (not isinstance(repo, dict) or repo.get('id') != repo_id or repo.get('private') is not False or
        repo.get('visibility') != 'public' or
        not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo.get('full_name', ''))):
        raise HTTPException(404, 'Public GitHub repository not available')
    return {'id': repo['id'], 'full_name': repo['full_name'], 'url': 'https://github.com/' + repo['full_name']}


def item_output(kind, item, repo):
    if kind == 'commit':
        ref = item.get('sha', '')
        if not re.fullmatch(r'[0-9a-f]{40,64}', ref):
            raise HTTPException(502, 'Unexpected GitHub commit response')
        title = item.get('commit', {}).get('message', '').split('\n')[0][:500]
    else:
        ref = str(item.get('number', ''))
        if not re.fullmatch(r'[1-9][0-9]{0,11}', ref):
            raise HTTPException(502, 'Unexpected GitHub pull request response')
        # Never collect branch metadata, bodies, code, email addresses, or author details.
        title = str(item.get('title', ''))[:500]
    segment = 'commit' if kind == 'commit' else 'pull'
    return {'kind': kind, 'reference': ref, 'title': title, 'repository': repo['full_name'],
        'url': f"{repo['url']}/{segment}/{ref}"}


@router.post('/github/connect')
def connect(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lock_user(db, user)
    if db.get(GitHubConnection, user.id):
        raise HTTPException(409, 'Disconnect the existing GitHub account before connecting another')
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    with oauth_client() as client:
        url, _ = client.create_authorization_url(AUTHORIZE, state=state, code_verifier=verifier)
    # Explicit empty scope, rather than implicitly inheriting a previous grant.
    parts = urlsplit(url)
    url = urlunsplit(parts._replace(query=urlencode(parse_qsl(parts.query) + [('scope', ''), ('prompt', 'select_account')])))
    db.execute(delete(GitHubOAuthState).where((GitHubOAuthState.user_id == user.id) | (GitHubOAuthState.expires_at <= utc_now())))
    db.add(GitHubOAuthState(state_hash=token_hash(state), user_id=user.id,
        auth_session_hash=token_hash(request.cookies[COOKIE]), encrypted_verifier=cipher().encrypt(verifier.encode()).decode(),
        expires_at=utc_now() + timedelta(minutes=10)))
    db.commit()
    return {'authorization_url': url}


@router.get('/github/callback')
def callback(request: Request, state: str = Query('', max_length=200), code: str = Query('', max_length=500),
             error: str = Query('', max_length=200), db: Session = Depends(get_db), user: User = Depends(current_user)):
    # Atomic consumption prevents replay; bind to BOTH the user and their current login.
    attempt = db.execute(delete(GitHubOAuthState).where(
        GitHubOAuthState.state_hash == token_hash(state), GitHubOAuthState.user_id == user.id,
        GitHubOAuthState.auth_session_hash == token_hash(request.cookies[COOKIE]),
        GitHubOAuthState.expires_at > utc_now()).returning(GitHubOAuthState.encrypted_verifier)).scalar_one_or_none()
    db.commit()
    if not attempt:
        raise HTTPException(400, 'GitHub connection request expired or is invalid. Start Connect GitHub again.')
    frontend = os.getenv('FRONTEND_URL', 'http://127.0.0.1:5173').rstrip('/')
    if error or not code:
        return RedirectResponse(frontend + '/?github=denied', status_code=303)
    new_token = None
    try:
        lock_user(db, user)
        if db.get(GitHubConnection, user.id):
            raise HTTPException(409, 'GitHub already connected')
        with oauth_client() as client:
            token = client.fetch_token(TOKEN, code=code, code_verifier=decrypt(attempt), grant_type='authorization_code')
        new_token = token['access_token']
        # Refuse unexpectedly broad grants rather than retaining extra permissions.
        if token.get('scope', '').strip():
            raise HTTPException(409, 'Unexpected GitHub permissions')
        identity = api('/user', token=new_token)
        if not isinstance(identity.get('id'), int) or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', identity.get('login', '')):
            raise HTTPException(502, 'Unexpected GitHub identity response')
        expires = utc_now() + timedelta(seconds=int(token['expires_in'])) if token.get('expires_in') else None
        db.add(GitHubConnection(user_id=user.id, github_user_id=identity['id'], username=identity['login'],
            encrypted_token=cipher().encrypt(new_token.encode()).decode(), expires_at=expires))
        db.commit()
    except (AuthlibBaseError, httpx.HTTPError, HTTPException, IntegrityError, KeyError, ValueError):
        db.rollback()
        if new_token:
            revoke(new_token)
        return RedirectResponse(frontend + '/?github=failed', status_code=303)
    return RedirectResponse(frontend + '/?github=connected', status_code=303)


@router.get('/github/status')
def status(db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.get(GitHubConnection, user.id)
    return {'connected': row is not None, 'username': row.username if row else None,
        'connected_at': row.connected_at if row else None,
        'expired': bool(row and row.expires_at and row.expires_at <= utc_now())}


@router.post('/github/disconnect')
def disconnect(db: Session = Depends(get_db), user: User = Depends(current_user)):
    lock_user(db, user)
    row = db.get(GitHubConnection, user.id)
    revoked = True
    if row:
        try:
            revoked = revoke(decrypt(row.encrypted_token))
        except HTTPException:
            revoked = False
        db.delete(row)
    projects = select(Project.id).where(Project.user_id == user.id)
    sessions = select(BuildSession.id).where(BuildSession.project_id.in_(projects))
    db.execute(delete(GitHubEvidence).where(GitHubEvidence.session_id.in_(sessions)))
    db.execute(update(Project).where(Project.user_id == user.id).values(github_repo_id=None))
    db.execute(delete(GitHubOAuthState).where(GitHubOAuthState.user_id == user.id))
    db.commit()
    return {'connected': False, 'revoked': revoked,
        'message': 'Disconnected. Repository links and evidence removed; build sessions retained.' if revoked else
        'Disconnected locally. GitHub revocation could not be confirmed; revoke Pseudo in GitHub Settings > Applications.'}


@router.get('/github/repos')
def repos(page: int = Query(1, ge=1, le=1000), db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, token = connection(db, user)
    rows = api('/user/repos', token, {'visibility': 'public', 'affiliation': 'owner,collaborator,organization_member',
        'sort': 'updated', 'per_page': 30, 'page': page})
    return {'repositories': [{'id': row['id'], 'full_name': row['full_name']} for row in rows
        if row.get('private') is False and row.get('visibility', 'public') == 'public'], 'has_more': len(rows) == 30}


class LinkInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repo_id: int | None = Field(default=None, gt=0)


@router.get('/projects/{project_id}/github')
def project_repo(project_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = owned_project(db, user, project_id)
    return {'repository': public_repo(project.github_repo_id, connection(db, user)[1]) if project.github_repo_id else None}


@router.post('/projects/{project_id}/github')
def link_repo(project_id: str, data: LinkInput, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lock_user(db, user)
    project = owned_project(db, user, project_id)
    repo = None
    if data.repo_id is not None:
        _, token = connection(db, user)
        repo = public_repo(data.repo_id, token)
    project.github_repo_id = data.repo_id
    db.commit()
    # Existing evidence keeps its original repository identity when a project is relinked.
    return {'repository': repo}


@router.get('/projects/{project_id}/github/activity')
def activity(project_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = owned_project(db, user, project_id)
    _, token = connection(db, user)
    if not project.github_repo_id:
        raise HTTPException(409, 'Link a public GitHub repository first')
    repo = public_repo(project.github_repo_id, token)
    # The no-scope token can read public activity only; repo visibility is checked above.
    def commits():
        try:
            return api(f"/repos/{repo['full_name']}/commits", token=token, params={'per_page': 20})
        except HTTPException as exc:
            if exc.status_code == 409:
                return []
            raise
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(commits)
        pulls = api(f"/repos/{repo['full_name']}/pulls", token=token, params={'state': 'all', 'sort': 'updated', 'direction': 'desc', 'per_page': 20})
        changes = future.result()
    return {'repository': repo, 'items': [item_output('commit', row, repo) for row in changes] +
        [item_output('pull_request', row, repo) for row in pulls]}


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['commit', 'pull_request']
    reference: str = Field(min_length=1, max_length=64)


@router.post('/sessions/{session_id}/github-evidence', status_code=201)
def attach(session_id: str, data: EvidenceInput, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lock_user(db, user)
    session = owned_session(db, user, session_id)
    if session.end_time is None:
        raise HTTPException(409, 'Finish the session before attaching evidence')
    project = owned_project(db, user, session.project_id)
    _, token = connection(db, user)
    if not project.github_repo_id:
        raise HTTPException(409, 'Link a public GitHub repository first')
    if db.scalar(select(func.count()).select_from(GitHubEvidence).where(GitHubEvidence.session_id == session_id)) >= 20:
        raise HTTPException(409, 'A session can have at most 20 evidence items')
    pattern = r'[0-9a-f]{40,64}' if data.kind == 'commit' else r'[1-9][0-9]{0,11}'
    if not re.fullmatch(pattern, data.reference):
        raise HTTPException(422, 'Invalid GitHub evidence reference')
    repo = public_repo(project.github_repo_id, token)
    segment = 'commits' if data.kind == 'commit' else 'pulls'
    item = item_output(data.kind, api(f"/repos/{repo['full_name']}/{segment}/{data.reference}", token=token), repo)
    if item['reference'] != data.reference:
        raise HTTPException(422, 'GitHub reference does not match')
    row = GitHubEvidence(session_id=session_id, repo_id=repo['id'], kind=data.kind, reference=data.reference, title=item['title'])
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'That evidence is already attached') from None
    return {'id': row.id, **item}


def visible_evidence(db, session_ids, include_ids=False):
    """Fail closed on GitHub errors/private repos, without breaking the build log."""
    if not session_ids:
        return {}
    rows = db.execute(select(GitHubEvidence, GitHubConnection).join(
        BuildSession, BuildSession.id == GitHubEvidence.session_id).join(
        Project, Project.id == BuildSession.project_id).join(
        GitHubConnection, GitHubConnection.user_id == Project.user_id).where(
        GitHubEvidence.session_id.in_(session_ids)).order_by(GitHubEvidence.created_at.desc()).limit(200)).all()
    credentials = {}
    for row, account in rows:
        key = (row.repo_id, account.user_id)
        if key in credentials or len(credentials) >= 20:
            continue
        try:
            credentials[key] = None if account.expires_at and account.expires_at <= utc_now() else decrypt(account.encrypted_token)
        except HTTPException:
            credentials[key] = None
    def check(key):
        try:
            token = credentials[key]
            return key, public_repo(key[0], token) if token else None
        except HTTPException:
            return key, None
    with ThreadPoolExecutor(max_workers=10) as pool:
        visible = dict(pool.map(check, credentials))
    result = {}
    for row, account in rows:
        repo = visible.get((row.repo_id, account.user_id))
        if repo is None:
            continue
        segment = 'commit' if row.kind == 'commit' else 'pull'
        item = {'kind': row.kind, 'reference': row.reference, 'title': row.title,
            'repository': repo['full_name'], 'url': f"{repo['url']}/{segment}/{row.reference}"}
        if include_ids:
            item['id'] = row.id
        result.setdefault(row.session_id, []).append(item)
    return result


@router.get('/sessions/{session_id}/github-evidence')
def session_evidence(session_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    owned_session(db, user, session_id)
    # IDs for removal are safe only in this owner-authenticated response.
    rows = db.scalars(select(GitHubEvidence).where(GitHubEvidence.session_id == session_id)).all()
    visible = visible_evidence(db, [session_id], include_ids=True).get(session_id, [])
    shown = {row['id'] for row in visible}
    return {'items': visible, 'unavailable': [{'id': row.id} for row in rows if row.id not in shown]}


@router.delete('/sessions/{session_id}/github-evidence/{evidence_id}')
def remove_evidence(session_id: str, evidence_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lock_user(db, user)
    owned_session(db, user, session_id)
    row = db.scalar(select(GitHubEvidence).where(GitHubEvidence.id == evidence_id, GitHubEvidence.session_id == session_id))
    if row is None:
        raise HTTPException(404, 'Evidence not found')
    db.delete(row)
    db.commit()
    return {'removed': True}
