import React, { useEffect, useState } from 'react';
import { request } from './api.js';
import { EvidenceList } from './GitHubPanel.jsx';

function formatTime(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return hours || minutes ? `${hours}h ${minutes}m` : `${seconds}s`;
}

export default function PublicProfile({ pseudonym }) {
  const [profile, setProfile] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError('');
    setProfile(null);
    request(`/profiles/${encodeURIComponent(pseudonym)}`, { credentials: 'omit' })
      .then((data) => { if (current) setProfile(data); })
      .catch((err) => { if (current) setError(err.status === 404 ? 'Profile not found.' : 'Could not load this profile. Please try again.'); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [pseudonym, attempt]);

  return <div className="app-shell public-profile">
    <header className="header"><a className="brand" href="/"><span className="brand-mark">p.</span>Pseudo</a><a className="secondary" href="/">Your dashboard / Login</a></header>
    <main>
      {loading ? <p className="empty" role="status">Loading public profile…</p> : error ? <section className="work-panel profile-error"><h1>{error}</h1><button className="secondary" onClick={() => setAttempt((value) => value + 1)}>Retry</button></section> : <>
        <section className="intro"><div><p className="eyebrow">PUBLIC BUILD HISTORY</p><h1>{profile.pseudonym}</h1><p className="intro-copy">The work, one session at a time.</p></div></section>
        <section className="stats" aria-label="Published statistics">
          <article className="stat-card"><p className="stat-number">{profile.total_projects}</p><p className="stat-note">{profile.total_projects === 1 ? 'project' : 'projects'} with published work</p></article>
          <article className="stat-card"><p className="stat-number">{(profile.total_duration_seconds / 3600).toFixed(2)}</p><p className="stat-note">documented hours · {formatTime(profile.total_duration_seconds)}</p></article>
          <article className="stat-card"><p className="stat-number">{profile.total_sessions}</p><p className="stat-note">published {profile.total_sessions === 1 ? 'session' : 'sessions'}</p></article>
        </section>
        <section className="build-log" aria-labelledby="public-log-title"><div className="log-header"><h2 id="public-log-title">Build Log</h2><span className="muted">Newest first</span></div>
          {profile.sessions.length === 0 ? <p className="empty">No published sessions yet.</p> : <ol className="session-list">{profile.sessions.map((session, index) => <li className="session" key={`${session.end_time}-${index}`}>
            <div className="session-date"><time dateTime={session.end_time}>{new Date(session.end_time).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}</time><span className="duration">{formatTime(session.duration_seconds)}</span></div>
            <div className="session-content"><p className="active-project">{session.project_name}</p><h3>{session.task}</h3>{session.accomplishment && <p>{session.accomplishment}</p>}{session.evidence?.length > 0 && <div><h4>GitHub evidence</h4><EvidenceList items={session.evidence} /><p className="stat-note">Evidence of activity, not proof of hours.</p></div>}</div>
          </li>)}</ol>}
        </section>
        <p className="footnote">Only work explicitly published by {profile.pseudonym} appears here.</p>
      </>}
    </main>
    <footer><span>Pseudo / Build in public. Stay private.</span></footer>
  </div>;
}
