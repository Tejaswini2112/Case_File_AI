import { REFUSAL_THRESHOLD, type Hit } from '../api'

// Plain-language names for the internal doc_kind values. The API speaks in
// pipeline terms; a reader should not have to.
const KIND_LABEL: Record<string, string> = {
  'newspaper': 'Newspaper clipping',
  'court-opinion': 'Court opinion',
  'teletype': 'Teletype message',
  'form': 'FBI form',
  'deletion-sheet': 'Removal notice',
  'legal': 'Legal document',
  'cover': 'Cover sheet',
  'memo': 'Memorandum',
  'loose': 'Loose page',
}

function pageLabel(hit: Hit): string {
  if (!hit.page_nos.length) return 'page unknown'
  if (hit.page_nos.length === 1) return `page ${hit.page_nos[0]}`
  return `pages ${hit.page_nos[0]}–${hit.page_nos[hit.page_nos.length - 1]}`
}

/** A labelled bar with the refusal cut-off marked, so a score reads as a
 *  position relative to the decision rather than as a bare number. */
function ScoreBar({ score }: { score: number }) {
  return (
    <div>
      <div className="flex justify-between items-baseline">
        <span className="text-sm">How closely it matches the question</span>
        <span className="font-mono text-sm">{score.toFixed(2)}</span>
      </div>
      <div className="relative h-1.5 bg-paper-2 rounded mt-2">
        <div
          className="absolute inset-y-0 left-0 bg-ink rounded"
          style={{ width: `${Math.min(score, 1) * 100}%` }}
        />
        <div
          className="absolute -top-1 -bottom-1 w-px bg-ink-faint"
          style={{ left: `${REFUSAL_THRESHOLD * 100}%` }}
          title={`Refusal cut-off (${REFUSAL_THRESHOLD})`}
        />
      </div>
      <p className="text-xs text-ink-faint mt-1.5">
        The mark is the cut-off. Below it the system refuses to answer at all.
      </p>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-sm text-ink-soft">{label}</dt>
      <dd className="text-sm text-right font-mono">{value}</dd>
    </>
  )
}

export default function PageInspector({ hit, onClose }: { hit: Hit; onClose: () => void }) {
  const kind = hit.doc_kind ?? 'unknown'

  return (
    <div className="h-full overflow-y-auto bg-paper-2 border-l border-rule p-8">
      <div className="flex items-start justify-between gap-4 mb-1">
        <h2 className="text-xl">
          {KIND_LABEL[kind] ?? kind}, {pageLabel(hit)}
        </h2>
        <button
          onClick={onClose}
          className="text-xs border border-rule rounded px-3 py-1 text-ink-soft
                     hover:text-ink hover:border-ink-faint shrink-0"
        >
          Close
        </button>
      </div>
      <p className="text-sm text-ink-soft mb-6">
        Retrieved as source {hit.rank} for this question
      </p>

      {/* Deliberately a placeholder. Rendering the scan needs page images from
          the source PDFs, which are not served yet — an empty frame is honest,
          a stock image would not be. */}
      <div className="border border-rule bg-panel h-44 mb-6 flex items-end p-2">
        <span className="text-xs text-ink-faint">page image not yet available</span>
      </div>

      <h3 className="text-sm mb-2">What the page says</h3>
      <pre className="bg-panel border border-rule rounded p-4 mb-6 text-xs font-mono
                      whitespace-pre-wrap leading-relaxed text-ink-soft">
        {hit.text}
      </pre>
      <p className="text-xs text-ink-faint -mt-4 mb-6">
        This is the excerpt the model was given &mdash; not the whole page.
      </p>

      <h3 className="text-sm mb-3">How much to trust this</h3>
      <ScoreBar score={hit.score} />

      <h3 className="text-sm mt-8 mb-3">Where this page came from</h3>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2">
        <Row label="Kind of document" value={KIND_LABEL[kind] ?? kind} />
        <Row label="Pages" value={hit.page_nos.join(', ') || '—'} />
        {hit.case_nums.length > 0 && (
          <Row label="Case number" value={hit.case_nums.join(', ')} />
        )}
        <Row label="Reference" value={hit.doc_id ?? '—'} />
      </dl>
    </div>
  )
}
