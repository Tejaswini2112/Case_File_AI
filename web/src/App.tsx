import { useEffect, useState } from 'react'
import { ApiError, ask, fetchCases, type AskResponse, type Cases, type Hit } from './api'
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
  const [cases, setCases] = useState<Cases>({})
  // Empty string means every case. Not null, so the select has a real value
  // and React does not treat it as uncontrolled.
  const [selectedCase, setSelectedCase] = useState('')

  // Non-fatal if it fails: the scope selector degrades to "All cases", which is
  // the default anyway, and asking questions still works.
  useEffect(() => {
    fetchCases().then(setCases).catch(() => {})
  }, [])

  async function handleAsk(question: string) {
    setSelected(null)
    setView({ state: 'loading', question })
    const started = performance.now()
    try {
      const result = await ask(question, { case: selectedCase || undefined })
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
      {/* Left: the brief. Scrolls on its own so opening a source does not move
          the reader's place in the answer. */}
      <div className="flex-1 min-w-0 overflow-y-auto">
        <div className="max-w-2xl mx-auto px-10 py-12">
          <div className="label-caps pb-4 mb-10 border-b border-ink">
            FBI files on Ted Bundy &middot; released under the Freedom of Information Act
          </div>

          {view.state === 'idle' && (
            <div className="mb-12">
              <h1 className="font-serif text-4xl leading-tight mb-5">
                What do these files say?
              </h1>
              <p className="font-serif text-[19px] leading-[1.75] text-ink-soft">
                Every answer is drawn only from the released documents, with each claim
                traced to the page it came from. Where the files are silent, you will be
                told so rather than given a guess.
              </p>
            </div>
          )}

          {view.state === 'loading' && (
            <div className="mb-12">
              <h1 className="font-serif text-4xl leading-tight mb-5">{view.question}</h1>
              <p className="text-ink-faint text-sm">Reading the files&hellip;</p>
            </div>
          )}

          {view.state === 'error' && (
            <div className="mb-12">
              <h1 className="font-serif text-4xl leading-tight mb-5">Something went wrong</h1>
              <p className="font-mono text-sm text-mark">{view.message}</p>
            </div>
          )}

          {view.state === 'answered' && (
            <div className="mb-12">
              <Brief
                result={view.result}
                elapsedSeconds={view.elapsedSeconds}
                selectedChunkId={selected?.chunk_id ?? null}
                onSelect={setSelected}
              />
            </div>
          )}

          <AskForm
            onAsk={handleAsk}
            busy={view.state === 'loading'}
            cases={cases}
            selectedCase={selectedCase}
            onSelectCase={setSelectedCase}
          />
        </div>
      </div>

      {/* Right: the inspector, mounted only when there is a source to inspect,
          so the brief keeps full width until a reader asks for the evidence. */}
      {selected && (
        <div className="w-[46%] max-w-2xl shrink-0">
          <PageInspector hit={selected} onClose={() => setSelected(null)} />
        </div>
      )}
    </div>
  )
}
