import { useState } from 'react'
import { ApiError, ask, type AskResponse, type Hit } from './api'
import AskForm from './components/AskForm'
import Brief from './components/Brief'
import PageInspector from './components/PageInspector'

type View =
  | { state: 'idle' }
  | { state: 'loading'; question: string }
  | { state: 'answered'; result: AskResponse; elapsedSeconds: number }
  | { state: 'error'; message: string }

export default function App() {
  const [view, setView] = useState<View>({ state: 'idle' })
  const [selected, setSelected] = useState<Hit | null>(null)

  async function handleAsk(question: string) {
    setSelected(null)
    setView({ state: 'loading', question })
    const started = performance.now()
    try {
      const result = await ask(question)
      setView({
        state: 'answered',
        result,
        elapsedSeconds: (performance.now() - started) / 1000,
      })
    } catch (err) {
      setView({
        state: 'error',
        message: err instanceof ApiError ? err.message : String(err),
      })
    }
  }

  return (
    <div className="h-full flex">
      {/* Left: the brief. Scrolls independently so opening a source does not
          move the reader's place in the answer. */}
      <div className="flex-1 min-w-0 overflow-y-auto">
        <div className="max-w-2xl mx-auto px-10 py-12">
          <p className="text-xs text-ink-faint mb-10">
            FBI files on Ted Bundy, released under the Freedom of Information Act
          </p>

          {view.state === 'idle' && (
            <div className="mb-10">
              <h1 className="text-3xl mb-3">What do these files say?</h1>
              <p className="text-ink-soft">
                Every answer is drawn only from the released documents, with each claim
                traced to the page it came from. Where the files are silent, you will be
                told so rather than given a guess.
              </p>
            </div>
          )}

          {view.state === 'loading' && (
            <div className="mb-10">
              <h1 className="text-3xl mb-3">{view.question}</h1>
              <p className="text-ink-soft">Reading the files&hellip;</p>
            </div>
          )}

          {view.state === 'error' && (
            <div className="mb-10">
              <h1 className="text-3xl mb-3">Something went wrong</h1>
              <p className="text-ink-soft font-mono text-sm">{view.message}</p>
            </div>
          )}

          {view.state === 'answered' && (
            <div className="mb-10">
              <Brief
                result={view.result}
                elapsedSeconds={view.elapsedSeconds}
                selectedChunkId={selected?.chunk_id ?? null}
                onSelect={setSelected}
              />
            </div>
          )}

          <AskForm onAsk={handleAsk} busy={view.state === 'loading'} />
        </div>
      </div>

      {/* Right: the inspector, present only when there is a source to inspect.
          Kept out of the tree entirely rather than hidden, so the brief gets
          the full width until a reader asks to see the evidence. */}
      {selected && (
        <div className="w-[46%] max-w-2xl shrink-0">
          <PageInspector hit={selected} onClose={() => setSelected(null)} />
        </div>
      )}
    </div>
  )
}
