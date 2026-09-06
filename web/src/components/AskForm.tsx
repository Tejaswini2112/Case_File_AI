import { useState } from 'react'

import type { Cases } from '../api'

const EXAMPLES = [
  'What evidence was found in the car?',
  'How did Bundy escape from custody?',
  'What is the capital of France?',
]

type Props = {
  onAsk: (question: string) => void
  busy: boolean
  cases: Cases
  selectedCase: string
  onSelectCase: (slug: string) => void
}

export default function AskForm({ onAsk, busy, cases, selectedCase, onSelectCase }: Props) {
  const [value, setValue] = useState('')

  function submit(question: string) {
    const q = question.trim()
    if (q && !busy) onAsk(q)
  }

  return (
    <div className="border-t border-ink pt-5">
      <form
        onSubmit={(e) => {
          e.preventDefault()
          submit(value)
        }}
        className="flex gap-0"
      >
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Ask something about these files"
          disabled={busy}
          className="flex-1 bg-panel border border-rule border-r-0 px-4 py-3 text-[15px]
                     outline-none focus:border-ink-faint disabled:opacity-60"
        />
        <button
          type="submit"
          disabled={busy || !value.trim()}
          className="bg-ink text-paper px-8 text-sm font-medium tracking-wide
                     disabled:opacity-30"
        >
          {busy ? 'Reading' : 'Ask'}
        </button>
      </form>

      {/* Scope is shown rather than implied. "All cases" is a real state with
          consequences once the corpus holds more than one investigation, and a
          reader should never be in it without knowing. */}
      <div className="flex items-baseline gap-2 mt-4">
        <span className="label-caps">Searching</span>
        <select
          value={selectedCase}
          onChange={(e) => onSelectCase(e.target.value)}
          disabled={busy}
          className="text-[13px] bg-transparent border-b border-rule py-0.5
                     hover:border-ink-faint focus:border-mark outline-none
                     disabled:opacity-40"
        >
          <option value="">All cases</option>
          {Object.entries(cases).map(([slug, name]) => (
            <option key={slug} value={slug}>{name}</option>
          ))}
        </select>
      </div>

      {/* The third example is out of corpus on purpose. A refusal is a feature
          of this system, and inviting one is the fastest way to demonstrate it. */}
      <div className="flex flex-wrap gap-x-5 gap-y-2 mt-4">
        {EXAMPLES.map((ex) => (
          <button
            key={ex}
            type="button"
            disabled={busy}
            onClick={() => {
              setValue(ex)
              submit(ex)
            }}
            className="text-[13px] text-ink-faint underline underline-offset-4
                       decoration-rule hover:text-mark hover:decoration-mark
                       disabled:opacity-40 disabled:no-underline"
          >
            {ex}
          </button>
        ))}
      </div>
    </div>
  )
}
