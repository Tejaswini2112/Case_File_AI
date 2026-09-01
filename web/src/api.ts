// Mirrors the pydantic models in src/api/app.py. Kept by hand rather than
// generated from /openapi.json: the schema is small and stable, and a codegen
// step would add a build dependency for four interfaces. If the response shape
// starts changing often, generate it instead of maintaining two copies.

export type Hit = {
  rank: number
  chunk_id: string
  doc_id: string | null
  doc_kind: string | null
  page_nos: number[]
  case_nums: string[]
  score: number
  text: string
}

export type Usage = {
  input_tokens: number
  output_tokens: number
  estimated_cost_usd: number
}

export type AskResponse = {
  question: string
  answer: string
  refused: boolean
  model: string
  hits: Hit[]
  usage: Usage
}

// The refusal threshold the API applies. Duplicated from REFUSAL_THRESHOLD in
// src/agents/ask.py so the inspector can draw the cut-off line on a score bar.
// A response does not report the threshold it used, so there is nothing to read
// it from; if the API ever returns it, prefer that over this constant.
export const REFUSAL_THRESHOLD = 0.3

/** Raised for a response the server understood and rejected, or could not fulfil. */
export class ApiError extends Error {
  // Declared and assigned rather than written as a constructor parameter
  // property: this project's tsconfig sets erasableSyntaxOnly, which restricts
  // TypeScript to syntax strippable without emitting JavaScript, and parameter
  // properties generate assignments.
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

type ValidationDetail = { loc: (string | number)[]; msg: string }

/**
 * Ask the corpus a question.
 *
 * Failures arrive in three shapes and are flattened into one readable message:
 * a 422 carries a list of per-field validation errors, a 502 carries a single
 * detail string describing an upstream failure, and a rejected fetch means the
 * API is unreachable — nearly always uvicorn not running, so the message says
 * so rather than surfacing a bare network error.
 */
export async function ask(question: string, docKind?: string): Promise<AskResponse> {
  let res: Response
  try {
    res = await fetch('/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(docKind ? { question, doc_kind: docKind } : { question }),
    })
  } catch {
    throw new ApiError('Cannot reach the API. Is uvicorn running on port 8000?', 0)
  }

  const body = await res.json().catch(() => null)

  if (!res.ok) {
    const detail = body?.detail
    if (Array.isArray(detail)) {
      const msg = (detail as ValidationDetail[])
        .map((d) => `${d.loc.slice(1).join('.')}: ${d.msg}`)
        .join('; ')
      throw new ApiError(msg, res.status)
    }
    throw new ApiError(detail ?? `Request failed (HTTP ${res.status})`, res.status)
  }

  return body as AskResponse
}

/**
 * Split an answer into plain text and the inline [doc-id, p.N] citations the
 * system prompt requires, so each citation can be rendered as a control that
 * opens its source rather than as punctuation in the middle of a sentence.
 *
 * A citation may name several sources at once, separated by semicolons
 * (`[a, p.1; b, p.2]`). Only the first is used as the click target: the
 * inspector shows one page at a time, and the first is the one the model
 * listed as primary support.
 */
export type Segment =
  | { kind: 'text'; text: string }
  | { kind: 'cite'; text: string; docId: string; page: number | null }

const CITATION_RE = /\[([^\][]*?p\.\d[^\][]*?)\]/g

export function parseAnswer(answer: string): Segment[] {
  const segments: Segment[] = []
  let last = 0

  for (const match of answer.matchAll(CITATION_RE)) {
    const start = match.index
    if (start > last) segments.push({ kind: 'text', text: answer.slice(last, start) })

    const first = match[1].split(';')[0]
    const [docId, page] = first.split(',').map((s) => s.trim())
    segments.push({
      kind: 'cite',
      text: match[0],
      docId: docId ?? '',
      page: page ? Number(page.replace(/^p\./, '')) || null : null,
    })
    last = start + match[0].length
  }

  if (last < answer.length) segments.push({ kind: 'text', text: answer.slice(last) })
  return segments
}
