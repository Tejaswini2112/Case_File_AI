import { useEffect, useState } from 'react'

type Health = { status: string; index: string }

// Three states rather than two: "checking" is a real state the user can see,
// and collapsing it into "down" would flash a false error on every load.
type Probe =
  | { state: 'checking' }
  | { state: 'up'; health: Health }
  | { state: 'down'; reason: string }

export default function App() {
  const [probe, setProbe] = useState<Probe>({ state: 'checking' })

  useEffect(() => {
    fetch('/health')
      .then(async (res) => {
        if (!res.ok) throw new Error(`API returned HTTP ${res.status}`)
        setProbe({ state: 'up', health: await res.json() })
      })
      // A rejected fetch here almost always means uvicorn is not running, so
      // the message names that rather than reporting a bare network error.
      .catch((err) => setProbe({ state: 'down', reason: String(err.message ?? err) }))
  }, [])

  return (
    <div className="min-h-full flex items-center justify-center p-10">
      <div className="w-full max-w-lg">
        <p className="text-xs tracking-wide text-ink-faint mb-2">
          FBI files on Ted Bundy, released under the Freedom of Information Act
        </p>
        <h1 className="text-3xl mb-8">CaseFile AI</h1>

        <div className="bg-panel border border-rule rounded p-5">
          <h2 className="text-xs uppercase tracking-widest text-ink-faint mb-3">
            Connection
          </h2>

          {probe.state === 'checking' && (
            <p className="text-ink-soft">Checking the API&hellip;</p>
          )}

          {probe.state === 'up' && (
            <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 font-mono text-sm">
              <dt className="text-ink-faint">status</dt>
              <dd className="text-right">{probe.health.status}</dd>
              <dt className="text-ink-faint">index</dt>
              <dd className="text-right">{probe.health.index}</dd>
            </dl>
          )}

          {probe.state === 'down' && (
            <div>
              <p className="mb-2">Cannot reach the API.</p>
              <p className="text-sm text-ink-soft mb-3 font-mono">{probe.reason}</p>
              <p className="text-sm text-ink-soft">
                Start it with{' '}
                <code className="font-mono text-xs bg-paper-2 px-1.5 py-0.5 rounded">
                  .venv\Scripts\python.exe -m uvicorn src.api.app:app
                </code>
              </p>
            </div>
          )}
        </div>

        <p className="text-xs text-ink-faint mt-4">
          Scaffold only &mdash; this page exists to confirm the dev server reaches FastAPI.
        </p>
      </div>
    </div>
  )
}
