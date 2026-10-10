"""Real PostgreSQL and real Authlib with mocked outbound GitHub HTTP only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from authlib.integrations.httpx_client import OAuth2Client
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from main import app
from database import get_db
from models import GitHubConnection, GitHubOAuthState
import github_integration as github

SHA = 'a' * 40


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL for PostgreSQL tests')
class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'github_test_' + uuid4().hex
        url = make_url(os.environ['TEST_DATABASE_URL']).set(drivername='postgresql+psycopg')
        self.admin = create_engine(url, hide_parameters=True)
        with self.admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.addCleanup(self.clean_schema)
        self.url = url.update_query_dict({'options': f'-csearch_path={self.schema} -ctimezone=UTC'})
        env = dict(os.environ, DATABASE_URL=self.url.render_as_string(hide_password=False))
        result = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
            cwd=Path(__file__).parent, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.engine = create_engine(self.url, hide_parameters=True)
        def database():
            with Session(self.engine, expire_on_commit=False) as db:
                yield db
        app.dependency_overrides[get_db] = database
        self.addCleanup(app.dependency_overrides.clear)
        self.env = patch.dict(os.environ, {'GITHUB_CLIENT_ID': 'test-client', 'GITHUB_CLIENT_SECRET': 'test-secret',
            'GITHUB_TOKEN_KEY': Fernet.generate_key().decode(), 'COOKIE_SECURE': 'false',
            'GITHUB_CALLBACK_URL': 'http://testserver/github/callback', 'FRONTEND_URL': 'http://testserver'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.requests = []
        self.private = False
        self.unavailable = False
        self.token_failure = False
        self.broad = False
        self.revocation_failure = False
        self.empty = False
        self.transport = httpx.MockTransport(self.github_response)
        client_patch = patch.object(github, 'http_client', lambda: httpx.Client(transport=self.transport))
        client_patch.start(); self.addCleanup(client_patch.stop)
        oauth_patch = patch.object(github, 'oauth_client', lambda: OAuth2Client('test-client', 'test-secret',
            redirect_uri='http://testserver/github/callback', token_endpoint_auth_method='client_secret_post',
            code_challenge_method='S256', headers={'Accept': 'application/json'}, transport=self.transport))
        oauth_patch.start(); self.addCleanup(oauth_patch.stop)
        self.a = TestClient(app, follow_redirects=False, headers={'X-Pseudo-Request': '1'})
        self.b = TestClient(app, follow_redirects=False, headers={'X-Pseudo-Request': '1'})
        self.public = TestClient(app, follow_redirects=False)
        for client in (self.a, self.b, self.public):
            self.addCleanup(client.close)
        self.a.post('/auth/register', json={'email': 'a@example.com', 'password': 'Test password 123!', 'pseudonym': 'GitBuilderA'}).raise_for_status()
        self.b.post('/auth/register', json={'email': 'b@example.com', 'password': 'Test password 123!', 'pseudonym': 'GitBuilderB'}).raise_for_status()

    def clean_schema(self):
        if hasattr(self, 'engine'):
            self.engine.dispose()
        with self.admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()

    def github_response(self, request):
        self.requests.append(request)
        path = request.url.path
        repo = {'id': 123, 'full_name': 'example/pseudo', 'private': self.private, 'visibility': 'private' if self.private else 'public'}
        if path == '/login/oauth/access_token':
            form = parse_qs(request.content.decode())
            self.assertEqual(form['client_secret'], ['test-secret'])
            self.assertIn('code_verifier', form)
            if self.token_failure:
                return httpx.Response(400, json={'error': 'bad_verification_code'})
            return httpx.Response(200, json={'access_token': 'mock-secret-token', 'token_type': 'bearer', 'scope': 'repo' if self.broad else ''})
        if path == '/user':
            self.assertEqual(request.headers['Authorization'], 'Bearer mock-secret-token')
            return httpx.Response(200, json={'id': 42, 'login': 'example', 'email': 'NEVER-EXPOSE'})
        if path == '/user/repos':
            self.assertEqual(request.url.params['visibility'], 'public')
            return httpx.Response(200, json=[repo, {'id': 999, 'full_name': 'SECRET/private', 'private': True}])
        if path == '/applications/test-client/token':
            self.assertEqual(request.method, 'DELETE')
            return httpx.Response(503 if self.revocation_failure else 204)
        if path == '/repositories/123':
            self.assertEqual(request.headers['Authorization'], 'Bearer mock-secret-token')
            return httpx.Response(503 if self.unavailable else 200, json=repo)
        self.assertEqual(request.headers['Authorization'], 'Bearer mock-secret-token')
        commit = {'sha': SHA, 'commit': {'message': 'Implement accounts\nBody not stored', 'author': {'email': 'NEVER-EXPOSE'}}}
        pr = {'number': 12, 'title': 'Add authentication', 'head': {'repo': {'private': True, 'full_name': 'NEVER-EXPOSE'}}}
        if path == '/repos/example/pseudo/commits':
            return httpx.Response(409 if self.empty else 200, json={} if self.empty else [commit])
        if path == '/repos/example/pseudo/pulls':
            return httpx.Response(200, json=[] if self.empty else [pr])
        if path == f'/repos/example/pseudo/commits/{SHA}':
            return httpx.Response(200, json=commit)
        if path == '/repos/example/pseudo/pulls/12':
            return httpx.Response(200, json=pr)
        self.fail('Unexpected outbound GitHub endpoint: ' + path)

    def begin(self, client=None):
        response = (client or self.a).post('/github/connect', json={})
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()['authorization_url']
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        self.assertEqual(query['scope'], [''])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertNotIn('test-secret', url)
        return query['state'][0]

    def connect(self):
        state = self.begin()
        response = self.a.get('/github/callback', params={'state': state, 'code': 'mock-code'})
        self.assertEqual(response.status_code, 303, response.text)
        self.assertTrue(response.headers['location'].endswith('github=connected'), response.headers)
        return state

    def project_session(self):
        project = self.a.post('/projects', json={'name': 'Pseudo', 'description': 'Private project notes'}).json()
        session = self.a.post('/sessions', json={'project_id': project['id'], 'task': 'Build accounts'}).json()
        self.a.post(f"/sessions/{session['id']}/finish", json={'accomplishment': 'Done'}).raise_for_status()
        return project['id'], session['id']

    def test_connection_repositories_link_activity_evidence_and_isolation(self):
        self.assertEqual(self.public.get('/github/status').status_code, 401)
        self.assertEqual(self.public.post('/github/connect', json={}, headers={'X-Pseudo-Request': '1'}).status_code, 401)
        self.assertEqual(self.a.post('/github/connect', json={}, headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.connect()
        status = self.a.get('/github/status').json()
        self.assertTrue(status['connected']); self.assertEqual(status['username'], 'example')
        self.assertNotIn('token', json.dumps(status)); self.assertNotIn('email', status)
        with Session(self.engine) as db:
            row = db.scalar(select(GitHubConnection))
            self.assertNotIn('mock-secret-token', row.encrypted_token)
            self.assertEqual(github.decrypt(row.encrypted_token), 'mock-secret-token')
        self.assertFalse(self.b.get('/github/status').json()['connected'])
        self.assertEqual(self.b.get('/github/repos').status_code, 409)
        repos = self.a.get('/github/repos').json()
        self.assertEqual(repos['repositories'], [{'id': 123, 'full_name': 'example/pseudo'}])
        project, session = self.project_session()
        link = f'/projects/{project}/github'
        self.assertEqual(self.b.post(link, json={'repo_id': 123}).status_code, 404)
        self.assertEqual(self.b.get(link + '/activity').status_code, 404)
        self.assertEqual(self.a.post(link, json={'repo_id': 123}).status_code, 200)
        activity = self.a.get(link + '/activity').json()['items']
        self.assertEqual([r['kind'] for r in activity], ['commit', 'pull_request'])
        self.assertNotIn('NEVER-EXPOSE', json.dumps(activity))
        path = f'/sessions/{session}/github-evidence'
        for kind, reference in [('commit', SHA), ('pull_request', '12')]:
            self.assertEqual(self.b.post(path, json={'kind': kind, 'reference': reference}).status_code, 404)
            response = self.a.post(path, json={'kind': kind, 'reference': reference})
            self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.a.post(path, json={'kind': 'commit', 'reference': SHA}).status_code, 409)
        self.assertEqual(self.a.post(path, json={'kind': 'commit', 'reference': '../secret'}).status_code, 422)
        self.assertEqual(self.public.get('/profiles/GitBuilderA').json()['sessions'], [])
        self.a.post(f'/sessions/{session}/publish', json={}).raise_for_status()
        public = self.public.get('/profiles/GitBuilderA').json()
        evidence = public['sessions'][0]['evidence']
        self.assertEqual(len(evidence), 2)
        self.assertEqual(set(evidence[0]), {'kind', 'reference', 'title', 'repository', 'url'})
        for secret in ('mock-secret-token', 'test-secret', 'NEVER-EXPOSE', 'Private project notes', 'a@example.com'):
            self.assertNotIn(secret, json.dumps(public))
        # Existing sessions/totals remain unchanged by evidence.
        self.assertEqual(public['total_sessions'], 1)
        self.assertEqual(self.a.get('/sessions').json()['total_sessions'], 1)
        # New client login proves all links/evidence are DB-backed, not client state.
        self.a.post('/auth/logout', json={})
        self.a.post('/auth/login', json={'email': 'a@example.com', 'password': 'Test password 123!'})
        self.assertEqual(self.a.get(link).json()['repository']['id'], 123)
        self.assertEqual(len(self.a.get(path).json()['items']), 2)
        item_id = self.a.get(path).json()['items'][0]['id']
        self.assertEqual(self.b.delete(path + '/' + item_id).status_code, 404)
        self.assertEqual(self.b.get(path).status_code, 404)
        self.assertEqual(self.a.delete(path + '/' + item_id).status_code, 200)
        self.assertEqual(len(self.public.get('/profiles/GitBuilderA').json()['sessions'][0]['evidence']), 1)
        self.b.post('/github/disconnect', json={})
        self.assertTrue(self.a.get('/github/status').json()['connected'])
        self.assertTrue(self.a.post('/github/disconnect', json={}).json()['revoked'])
        self.assertFalse(self.a.get('/github/status').json()['connected'])
        self.assertIsNone(self.a.get(link).json()['repository'])
        self.assertEqual(self.public.get('/profiles/GitBuilderA').json()['sessions'][0]['evidence'], [])
        self.assertEqual(self.a.get('/sessions').json()['total_sessions'], 1)

    def test_private_repositories_fail_closed_and_empty_activity(self):
        self.connect(); project, session = self.project_session()
        link = f'/projects/{project}/github'
        self.private = True
        self.assertEqual(self.a.post(link, json={'repo_id': 123}).status_code, 404)
        self.private = False
        self.a.post(link, json={'repo_id': 123})
        self.empty = True
        self.assertEqual(self.a.get(link + '/activity').json()['items'], [])
        self.empty = False
        path = f'/sessions/{session}/github-evidence'
        self.a.post(path, json={'kind': 'commit', 'reference': SHA}).raise_for_status()
        self.a.post(f'/sessions/{session}/publish', json={})
        self.private = True
        profile = self.public.get('/profiles/GitBuilderA').json()
        self.assertEqual(profile['sessions'][0]['evidence'], [])
        self.assertNotIn('example/pseudo', json.dumps(profile))
        self.assertNotIn('Implement accounts', json.dumps(profile))
        self.assertEqual(self.a.get(link + '/activity').status_code, 404)
        self.assertEqual(self.a.post(path, json={'kind': 'pull_request', 'reference': '12'}).status_code, 404)
        self.private = False; self.unavailable = True
        self.assertEqual(self.public.get('/profiles/GitBuilderA').json()['sessions'][0]['evidence'], [])
        self.revocation_failure = True
        result = self.a.post('/github/disconnect', json={})
        self.assertFalse(result.json()['revoked'])
        self.assertFalse(self.a.get('/github/status').json()['connected'])

    def test_oauth_state_replay_account_binding_expiry_and_denial(self):
        state = self.begin()
        self.assertEqual(self.b.get('/github/callback', params={'state': state, 'code': 'mock-code'}).status_code, 400)
        self.assertEqual(self.a.get('/github/callback', params={'state': 'wrong', 'code': 'mock-code'}).status_code, 400)
        response = self.a.get('/github/callback', params={'state': state, 'error': 'access_denied'})
        self.assertTrue(response.headers['location'].endswith('github=denied'))
        self.assertEqual(self.a.get('/github/callback', params={'state': state, 'code': 'mock-code'}).status_code, 400)
        state = self.begin()
        with self.engine.begin() as db:
            db.execute(text("UPDATE github_oauth_states SET expires_at=now()-interval '1 minute'"))
        self.assertEqual(self.a.get('/github/callback', params={'state': state, 'code': 'mock-code'}).status_code, 400)
        state = self.begin()
        self.a.post('/auth/logout', json={})
        self.a.post('/auth/login', json={'email': 'a@example.com', 'password': 'Test password 123!'})
        self.assertEqual(self.a.get('/github/callback', params={'state': state, 'code': 'mock-code'}).status_code, 400)
        state = self.connect()
        self.assertEqual(self.a.get('/github/callback', params={'state': state, 'code': 'mock-code'}).status_code, 400)
        self.assertEqual(self.a.post('/github/connect', json={}).status_code, 409)

    def test_oauth_failures_do_not_store_tokens_or_accept_broad_scopes(self):
        self.token_failure = True
        state = self.begin()
        self.assertTrue(self.a.get('/github/callback', params={'state': state, 'code': 'mock-code'}).headers['location'].endswith('github=failed'))
        self.assertFalse(self.a.get('/github/status').json()['connected'])
        self.token_failure = False; self.broad = True
        state = self.begin()
        self.assertTrue(self.a.get('/github/callback', params={'state': state, 'code': 'mock-code'}).headers['location'].endswith('github=failed'))
        self.assertFalse(self.a.get('/github/status').json()['connected'])
        self.assertTrue(any(r.method == 'DELETE' for r in self.requests))
        with Session(self.engine) as db:
            self.assertIsNone(db.scalar(select(GitHubConnection)))
            self.assertIsNone(db.scalar(select(GitHubOAuthState)))


if __name__ == '__main__':
    unittest.main()
