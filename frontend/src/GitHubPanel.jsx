import React, { useEffect, useState } from 'react';
import { post, request } from './api.js';

export function EvidenceList({ items }) {
  if (!items?.length) return null;
  return <ul className="evidence-list">{items.map((item) => <li key={`${item.repository}-${item.kind}-${item.reference}`}>
    <a href={item.url} target="_blank" rel="noopener noreferrer">{item.kind === 'commit' ? `Commit ${item.reference.slice(0, 7)}` : `PR #${item.reference}`} — {item.title}</a>
    <span className="muted">{item.repository}</span>
  </li>)}</ul>;
}

export default function GitHubPanel({ project, sessions = [], expire }) {
  const [status, setStatus] = useState(null);
  const [repos, setRepos] = useState([]);
  const [page, setPage] = useState(0);
  const [more, setMore] = useState(false);
  const [repoId, setRepoId] = useState('');
  const [linked, setLinked] = useState(null);
  const [activity, setActivity] = useState(null);
  const [sessionId, setSessionId] = useState('');
  const [evidence, setEvidence] = useState({ items: [], unavailable: [] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState(() => ({
    connected: 'GitHub connected successfully.', denied: 'GitHub authorization was cancelled. You can try again.',
    failed: 'GitHub connection failed. Check the OAuth configuration, existing connections, and permissions, then retry.',
  })[new URLSearchParams(window.location.search).get('github')] || '');
  const projectSessions = sessions.filter((s) => s.project_id === project?.id);
  const selectedSession = projectSessions.find((s) => s.id === sessionId);

  useEffect(() => {
    let current = true;
    request('/github/status').then((data) => { if (current) setStatus(data); }).catch((err) => {
      if (current) { setError(err.message); if (err.status === 401) expire(); }
    });
    if (project) request(`/projects/${project.id}/github`).then((data) => { if (current) setLinked(data.repository); })
      .catch((err) => { if (current) setError(err.message); });
    return () => { current = false; };
  }, [project?.id]);

  useEffect(() => {
    let current = true;
    setEvidence({ items: [], unavailable: [] });
    if (sessionId) request(`/sessions/${sessionId}/github-evidence`).then((data) => { if (current) setEvidence(data); })
      .catch((err) => { if (current) setError(err.message); });
    return () => { current = false; };
  }, [sessionId]);

  async function run(action) {
    setBusy(true); setError(''); setMessage('');
    try { await action(); } catch (err) { setError(err.message); if (err.status === 401) expire(); }
    finally { setBusy(false); }
  }
  async function loadRepos(nextPage) {
    const data = await request(`/github/repos?page=${nextPage}`);
    setRepos((previous) => nextPage === 1 ? data.repositories : [...previous, ...data.repositories.filter((row) => !previous.some((old) => old.id === row.id))]);
    setMore(data.has_more); setPage(nextPage);
  }
  async function remove(id) {
    await request(`/sessions/${sessionId}/github-evidence/${id}`, { method: 'DELETE' });
    setEvidence(await request(`/sessions/${sessionId}/github-evidence`));
  }

  return <section className="work-panel github-panel" aria-labelledby="github-title">
    <h2 id="github-title">GitHub</h2>
    <p className="stat-note">Attach selected public commits and pull requests to your work. Activity is evidence, not proof of hours.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {!status ? <p>Checking GitHub connection…</p> : !status.connected ? <button className="secondary" disabled={busy} onClick={() => run(async () => {
      const data = await post('/github/connect', {}); window.location.assign(data.authorization_url);
    })}>Connect GitHub</button> : <>
      <div className="section-heading"><p>Connected as <strong>@{status.username}</strong>{status.expired && ' — authorization expired; reconnect.'}</p>
        <button className="secondary" disabled={busy} onClick={() => run(async () => {
          const data = await post('/github/disconnect', {}); setStatus({ connected: false }); setLinked(null);
          setRepos([]); setActivity(null); setEvidence({ items: [], unavailable: [] }); setSessionId(''); setMessage(data.message);
        })}>Disconnect</button></div>
      <p className="stat-note">Disconnect removes repository links and attached GitHub evidence. Your projects, sessions, and recorded time remain.</p>
      <button className="secondary" disabled={busy} onClick={() => run(() => loadRepos(1))}>Load public repositories</button>
      {page > 0 && repos.length === 0 && <p>No public repositories found.</p>}
      {repos.length > 0 && <ul className="repo-list">{repos.map((repo) => <li key={repo.id}>{repo.full_name}</li>)}</ul>}
      {more && <button className="secondary" disabled={busy} onClick={() => run(() => loadRepos(page + 1))}>Load more repositories</button>}
      {!project ? <p>Create or select a Pseudo project to link a repository.</p> : <div className="github-project">
        <h3>Repository for {project.name}</h3>
        <p>{linked ? `Linked to ${linked.full_name}` : 'No available public repository linked.'}</p>
        <form onSubmit={(e) => { e.preventDefault(); run(async () => {
          const data = await post(`/projects/${project.id}/github`, { repo_id: Number(repoId) }); setLinked(data.repository); setActivity(null);
        }); }}>
          <label htmlFor="github-repo">GitHub repository</label>
          <select id="github-repo" value={repoId} onChange={(e) => setRepoId(e.target.value)} disabled={busy}>
            <option value="">Select a public repository</option>{repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.full_name}</option>)}
          </select><button className="secondary" disabled={busy || !repoId}>Link repository</button>
        </form>
        {linked && <button className="secondary" disabled={busy} onClick={() => run(async () => {
          await post(`/projects/${project.id}/github`, { repo_id: null }); setLinked(null); setActivity(null);
        })}>Unlink repository</button>}
        <p className="stat-note">Relinking or unlinking keeps already attached evidence associated with its original repository.</p>
        <label htmlFor="evidence-session">Completed session for evidence</label>
        <select id="evidence-session" value={sessionId} disabled={busy} onChange={(e) => setSessionId(e.target.value)}>
          <option value="">Select a completed session</option>{projectSessions.map((s) => <option key={s.id} value={s.id}>{s.task} — {new Date(s.end_time).toLocaleDateString()}</option>)}
        </select>
        <p className="publishing-note">Publishing a session also shares its attached evidence. GitHub links can reveal your GitHub identity. Review each item before attaching.{selectedSession?.is_published && <strong> This session is already public: attached evidence will appear publicly.</strong>}</p>
        {sessionId && <div><h3>Attached evidence</h3>
          {evidence.items.length === 0 && evidence.unavailable.length === 0 && <p>No evidence attached.</p>}
          {evidence.items.map((item) => <div key={item.id} className="evidence-item"><EvidenceList items={[item]} /><button className="secondary" disabled={busy} onClick={() => run(() => remove(item.id))}>Remove {item.kind === 'commit' ? item.reference.slice(0, 7) : `PR #${item.reference}`}</button></div>)}
          {evidence.unavailable.map((item) => <div key={item.id}><p>Evidence unavailable; hidden publicly because its repository could not be confirmed public.</p><button className="secondary" disabled={busy} onClick={() => run(() => remove(item.id))}>Remove unavailable evidence</button></div>)}
        </div>}
        <button className="secondary" disabled={busy || !linked} onClick={() => run(async () => setActivity(await request(`/projects/${project.id}/github/activity`)))}>Load recent commits and PRs</button>
        {activity && <div><p role="status">{activity.items.filter((item) => item.kind === 'commit').length} commits · {activity.items.filter((item) => item.kind === 'pull_request').length} pull requests retrieved.</p><p>Latest 20 default-branch commits and 20 recently updated PRs across all states. These may include other contributors; select only items relevant to this session.</p>
          {activity.items.length === 0 && <p>No commits or pull requests found.</p>}
          {activity.items.map((item) => { const attached = evidence.items.some((e) => e.repository === item.repository && e.kind === item.kind && e.reference === item.reference); return <div className="evidence-item" key={`${item.kind}-${item.reference}`}><EvidenceList items={[item]} />
            <button className="secondary" disabled={busy || !sessionId || attached} onClick={() => run(async () => {
              await post(`/sessions/${sessionId}/github-evidence`, { kind: item.kind, reference: item.reference });
              setEvidence(await request(`/sessions/${sessionId}/github-evidence`));
            })}>{attached ? 'Attached' : `Attach ${item.kind === 'commit' ? item.reference.slice(0, 7) : `PR #${item.reference}`}`}</button></div>; })}
        </div>}
      </div>}
    </>}
    {busy && <p role="status">Working with GitHub…</p>}
  </section>;
}
