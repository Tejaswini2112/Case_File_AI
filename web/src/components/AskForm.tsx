import { useState } from 'react'

const EXAMPLES = [
  'What evidence was found in the car?',
  'How did Bundy escape from custody?',
  'What is the capital of France?',
]

type Props = {
  onAsk: (question: string) => void
  busy: boolean
}

export default function AskForm({ onAsk, busy }: Props) {
  const [value, setValue] = useState('')

  function submit(question: string) {
    const q = question.trim()
    if (q && !busy) onAsk(q)
  }

  return (
    <div>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          submit(value)
        }}
        className="flex gap-2"
      >
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Ask something about these files"
          disabled={busy}
          className="flex-1 bg-panel border border-rule rounded px-4 py-3 outline-none
                     focus:border-ink-faint disabled:opacity-60"
        />
        <button
          type="submit"
          disabled={busy || !value.trim()}
          className="bg-ink text-paper px-6 rounded disabled:opacity-40"
        >
          {busy ? 'Reading' : 'Ask'}
        </button>
      </form>

      {/* The third example is an out-of-corpus question on purpose. A refusal is
          a feature of this system, and inviting a visitor to trigger one is the
          quickest way to show it works. */}
      <div className="flex flex-wrap gap-2 mt-3">
        {EXAMPLES.map((ex) => (
          <button
            key={ex}
            type="button"
            disabled={busy}
            onClick={() => {
              setValue(ex)
              submit(ex)
            }}
            className="text-xs text-ink-faint border border-rule rounded-full px-3 py-1
                       hover:text-ink hover:border-ink-faint disabled:opacity-40"
          >
            {ex}
          </button>
        ))}
      </div>
    </div>
  )
}
