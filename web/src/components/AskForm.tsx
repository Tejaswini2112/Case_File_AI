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
