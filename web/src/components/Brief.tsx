import { parseAnswer, type AskResponse, type Hit } from '../api'

type Props = {
  result: AskResponse
  elapsedSeconds: number
  selectedChunkId: string | null
  onSelect: (hit: Hit) => void
}

/** Resolve a citation to the chunk it names. A document can span several
 *  chunks, so the page narrows it; failing that, fall back to the document's
 *  best-ranked chunk. */
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
    <div className="flex flex-wrap gap-x-14 gap-y-5 pt-6 mt-10 border-t border-rule">
      {cells.map(([label, value]) => (
        <div key={label}>
          <div className="label-caps">{label}</div>
          <div className="font-mono text-sm mt-1.5">{value}</div>
        </div>
      ))}
    </div>
  )
}

export default function Brief({ result, elapsedSeconds, selectedChunkId, onSelect }: Props) {
  if (result.refused) {
    return (
      <div>
        <h1 className="font-serif text-4xl leading-tight mb-7">{result.question}</h1>
        <div className="border-l-2 border-mark bg-mark-soft px-5 py-4 mb-3">
          <div className="label-caps mb-2">These files cannot answer that</div>
          <p className="font-serif text-ink-soft leading-relaxed whitespace-pre-wrap">
            {result.answer}
          </p>
        </div>
        <p className="text-[13px] text-ink-faint">
          The system declined rather than guess. That is the intended behaviour, not a failure.
        </p>
        <Meta result={result} elapsedSeconds={elapsedSeconds} />
      </div>
    )
  }

  const segments = parseAnswer(result.answer)
  const citedDocs = new Set(segments.flatMap((s) => (s.kind === 'cite' ? [s.docId] : [])))

  return (
    <div>
      <h1 className="font-serif text-4xl leading-tight mb-4">{result.question}</h1>
      <p className="text-[13px] text-ink-faint mb-9 pb-6 border-b border-rule">
        Answered only from these files. Click any citation or source to read the page it came from.
      </p>

      <div className="font-serif text-[19px] leading-[1.75] whitespace-pre-wrap mb-11">
        {segments.map((seg, i) => {
          if (seg.kind === 'text') return <span key={i}>{seg.text}</span>
          const hit = resolveCitation(result.hits, seg.docId, seg.page)
          if (!hit) return <span key={i} className="text-ink-faint text-sm">{seg.text}</span>
          return (
            <button
              key={i}
              onClick={() => onSelect(hit)}
              title={`Open ${hit.doc_id}`}
              className="align-baseline font-sans text-[11px] text-mark mx-0.5
                         border-b border-rule hover:border-mark"
            >
              {seg.text}
            </button>
          )
        })}
      </div>

      <div className="label-caps mb-3">Sources retrieved</div>
      <div className="border-t border-rule">
        {result.hits.map((hit) => {
          const selected = hit.chunk_id === selectedChunkId
          // Retrieval returns the top k; the model cites what actually supported
          // the answer. Marking the difference avoids overstating the evidence.
          const cited = hit.doc_id !== null && citedDocs.has(hit.doc_id)
          return (
            <button
              key={hit.chunk_id}
              onClick={() => onSelect(hit)}
              className={`w-full text-left flex items-baseline gap-4 px-3 py-2.5
                          border-b border-rule hover:bg-paper-2
                          ${selected ? 'bg-paper-2 border-l-2 border-l-mark' : ''}`}
            >
              <span className="font-mono text-[11px] text-ink-faint w-3">{hit.rank}</span>
              <span className="text-sm">{hit.doc_id}</span>
              <span className="ml-auto flex items-baseline gap-4 text-[11px] font-mono text-ink-faint">
                {cited && <span className="text-mark">cited</span>}
                <span>p.{hit.page_nos.join(',') || '?'}</span>
                <span className="w-8 text-right">{hit.score.toFixed(2)}</span>
              </span>
            </button>
          )
        })}
      </div>

      <Meta result={result} elapsedSeconds={elapsedSeconds} />
    </div>
  )
}
