import { parseAnswer, type AskResponse, type Hit } from '../api'

type Props = {
  result: AskResponse
  elapsedSeconds: number
  selectedChunkId: string | null
  onSelect: (hit: Hit) => void
}

/** Find the hit a citation points at. Citations name a document and page, but
 *  a document can span several chunks, so the page narrows it; if nothing
 *  matches on page we fall back to the document's best-ranked chunk. */
function resolveCitation(hits: Hit[], docId: string, page: number | null): Hit | undefined {
  const inDoc = hits.filter((h) => h.doc_id === docId)
  if (!inDoc.length) return undefined
  if (page !== null) {
    const onPage = inDoc.find((h) => h.page_nos.includes(page))
    if (onPage) return onPage
  }
  return inDoc[0]
}

function Meta({ result, elapsedSeconds }: { result: AskResponse; elapsedSeconds: number }) {
  const cells: [string, string][] = [
    ['File text read', `${result.usage.input_tokens.toLocaleString()} tokens`],
    ['Answer written', `${result.usage.output_tokens.toLocaleString()} tokens`],
    ['Cost', `$${result.usage.estimated_cost_usd.toFixed(4)}`],
    ['Time', `${elapsedSeconds.toFixed(1)}s`],
  ]
  return (
    <div className="flex flex-wrap gap-x-12 gap-y-4 pt-6 mt-8 border-t border-rule">
      {cells.map(([label, value]) => (
        <div key={label}>
          <div className="text-xs text-ink-faint">{label}</div>
          <div className="font-mono text-sm mt-0.5">{value}</div>
        </div>
      ))}
    </div>
  )
}

export default function Brief({ result, elapsedSeconds, selectedChunkId, onSelect }: Props) {
  if (result.refused) {
    return (
      <div>
        <h1 className="text-3xl mb-6">{result.question}</h1>
        <div className="bg-paper-2 border border-rule rounded p-5 mb-2">
          <h2 className="text-sm mb-2">These files cannot answer that</h2>
          <p className="text-ink-soft leading-relaxed whitespace-pre-wrap">{result.answer}</p>
        </div>
        <p className="text-sm text-ink-faint">
          The system declined rather than guess. That is the intended behaviour, not a failure.
        </p>
        <Meta result={result} elapsedSeconds={elapsedSeconds} />
      </div>
    )
  }

  const segments = parseAnswer(result.answer)
  const citedDocs = new Set(
    segments.flatMap((s) => (s.kind === 'cite' ? [s.docId] : [])),
  )

  return (
    <div>
      <h1 className="text-3xl mb-4">{result.question}</h1>
      <p className="text-sm text-ink-soft mb-8">
        Answered only from these files. Click any citation or source to read the page it came from.
      </p>

      <div className="text-lg leading-relaxed whitespace-pre-wrap mb-10">
        {segments.map((seg, i) => {
          if (seg.kind === 'text') return <span key={i}>{seg.text}</span>
          const hit = resolveCitation(result.hits, seg.docId, seg.page)
          if (!hit) return <span key={i} className="text-ink-faint text-sm">{seg.text}</span>
          return (
            <button
              key={i}
              onClick={() => onSelect(hit)}
              title={`Open ${hit.doc_id}`}
              className="align-baseline text-xs font-mono px-1 py-0.5 mx-0.5 rounded
                         border border-rule bg-panel text-ink-soft hover:border-ink-faint hover:text-ink"
            >
              {seg.text}
            </button>
          )
        })}
      </div>

      <h2 className="text-xs uppercase tracking-widest text-ink-faint mb-3">
        Sources retrieved
      </h2>
      <div className="space-y-1.5">
        {result.hits.map((hit) => {
          const selected = hit.chunk_id === selectedChunkId
          // A retrieved chunk is not necessarily a used one: retrieval returns
          // the top k, and the model cites what actually supported the answer.
          // Marking the difference is more honest than implying all five were used.
          const cited = hit.doc_id !== null && citedDocs.has(hit.doc_id)
          return (
            <button
              key={hit.chunk_id}
              onClick={() => onSelect(hit)}
              className={`w-full text-left flex items-baseline gap-3 border rounded px-3 py-2
                          hover:border-ink-faint ${
                            selected ? 'border-ink-faint bg-paper-2' : 'border-rule bg-panel'
                          }`}
            >
              <span className="font-mono text-xs text-ink-faint">{hit.rank}</span>
              <span className="text-sm">{hit.doc_id}</span>
              <span className="ml-auto text-xs text-ink-faint font-mono">
                {cited && <span className="mr-3 not-italic">cited</span>}
                p.{hit.page_nos.join(',') || '?'} &middot; {hit.score.toFixed(2)}
              </span>
            </button>
          )
        })}
      </div>

      <Meta result={result} elapsedSeconds={elapsedSeconds} />
    </div>
  )
}
