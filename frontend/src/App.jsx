import { useEffect, useRef, useState } from 'react'
import Login from './Login'
import './App.css'

// Requests go to /api/*, which Vite proxies to the Flask backend (see vite.config.js).
const API_BASE = ''

const SINGLE_DOC_QUESTIONS = [
  'Summarize this document',
  'What information is missing?',
  'Are there any inconsistencies?',
  'What are the key dates and amounts?',
]

const MULTI_DOC_QUESTIONS = [
  'Does this invoice match the purchase order?',
  'Was everything on the PO delivered?',
  'What is the customer asking about?',
  'Which documents refer to the same order?',
]

const VERIFICATION = {
  verified: { label: 'Verified', tone: 'badge-ok' },
  partially_verified: { label: 'Partly verified', tone: 'badge-warn' },
  unverified: { label: 'Unverified', tone: 'badge-error' },
  not_in_documents: { label: 'Not in documents', tone: 'badge-muted' },
}

function formatLines(lines) {
  if (!lines?.length) return ''
  const first = lines[0]
  const last = lines[lines.length - 1]
  return lines.length > 1 && last - first === lines.length - 1 ? `L${first}–L${last}` : lines.map((n) => `L${n}`).join(', ')
}

let nextId = 1
const makeDoc = (name, text) => ({ id: nextId++, name, text })

function readFileAsText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error(`Unable to read ${file.name}.`))
    reader.readAsText(file)
  })
}

async function fileToDoc(file) {
  if (!file.name.toLowerCase().endsWith('.pdf')) return makeDoc(file.name, await readFileAsText(file))
  const formData = new FormData()
  formData.append('file', file)
  const response = await fetch(`${API_BASE}/api/upload`, { method: 'POST', body: formData })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || `Unable to read ${file.name}.`)
  return makeDoc(file.name, data.text)
}

async function postJson(path, body) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || 'Request failed.')
  return data
}

const formatLabel = (key) => key.replace(/_/g, ' ')
const formatQuote = (quote) => String(quote).split(/\n+/).map((part) => part.trim()).filter(Boolean).join(' · ')
const fileExt = (name) => (name.includes('.') ? name.split('.').pop().toUpperCase().slice(0, 4) : 'TXT')

function formatMoney(value, currency) {
  if (value === null || value === undefined) return 'Not found'
  const amount = Number(String(value).replace(/,/g, ''))
  if (Number.isNaN(amount)) return value
  const symbol = { INR: '₹', USD: '$' }[currency] ?? (currency ? `${currency} ` : '')
  return `${symbol}${amount.toLocaleString(currency === 'INR' ? 'en-IN' : 'en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function ModelBadge({ health }) {
  if (!health) return <span className="badge badge-muted">Backend offline</span>
  if (!health.llm_configured) return <span className="badge badge-warn">Rule-based only</span>
  return (
    <span className="badge badge-ok" title={health.model}>
      <span className="dot" /> {health.model.split('/').pop()}
    </span>
  )
}

function StatusBadge({ status }) {
  const tone = { MATCH: 'badge-ok', MISMATCH: 'badge-error', INSUFFICIENT_DATA: 'badge-warn', MISSING: 'badge-warn' }[status] || 'badge-muted'
  return <span className={`badge ${tone}`}>{formatLabel(status)}</span>
}

function ComparisonResult({ comparison }) {
  const { currency } = comparison
  return (
    <div className="comparison">
      <div className="stat-row">
        <div className="stat">
          <span>Invoice</span>
          <strong>{formatMoney(comparison.invoice_amount, currency)}</strong>
          {comparison.invoice_document && <small>{comparison.invoice_document}</small>}
        </div>
        <div className="stat">
          <span>Purchase order</span>
          <strong>{formatMoney(comparison.purchase_order_amount, currency)}</strong>
          {comparison.purchase_order_document && <small>{comparison.purchase_order_document}</small>}
        </div>
        <div className={`stat stat-status status-${comparison.status.toLowerCase()}`}>
          <span>Status</span>
          <strong>{formatLabel(comparison.status)}</strong>
        </div>
      </div>

      {comparison.checks?.length > 0 && (
        <table className="checks">
          <thead>
            <tr><th>Check</th><th>Invoice</th><th>Purchase order</th><th /></tr>
          </thead>
          <tbody>
            {comparison.checks.map((check) => (
              <tr key={check.field}>
                <td>{check.label}</td>
                {['invoice', 'purchase_order'].map((side) => (
                  <td key={side} title={check[side]?.evidence || ''}>
                    {check[side] ? (check.field === 'total_amount' ? formatMoney(check[side].value, currency) : check[side].value) : <em>not found</em>}
                    {check[side]?.evidence && <span className="cell-evidence">“{formatQuote(check[side].evidence)}”</span>}
                  </td>
                ))}
                <td><StatusBadge status={check.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="observations">
        <strong>Observations</strong>
        <ul>
          {comparison.observations.map((item) => <li key={item}>{item}</li>)}
        </ul>
      </div>
      <p className="fineprint">Values are extracted with quotes from each document and verified; the status is computed by code, not the model.</p>
    </div>
  )
}

function AnalysisView({ analysis }) {
  const fields = Object.entries(analysis.fields || {})
  return (
    <div>
      <p className="summary">{analysis.summary}</p>
      <p className="analysis-source">
        Extracted by the model · each field checked against the line it cites
      </p>
      {fields.length > 0 && (
        <dl className="field-grid">
          {fields.map(([key, value]) => (
            <div key={key} className={`field ${value.verified === false ? 'unverified' : ''}`}>
              <dt>
                {formatLabel(key)}
                {value.verified === false && <span className="tag" title="The value was not found on the line the model cited">unverified</span>}
              </dt>
              <dd title={value.value}>{value.value}</dd>
              {value.evidence && (
                <dd className="evidence" title={value.evidence}>
                  {value.lines?.length > 0 && <span className="line-ref">{formatLines(value.lines)}</span>} “{formatQuote(value.evidence)}”
                </dd>
              )}
            </div>
          ))}
        </dl>
      )}
      {analysis.inconsistencies?.length > 0 && (
        <div className="notice notice-error">
          <strong>Inconsistencies</strong>
          <ul>
            {analysis.inconsistencies.map((item) => (
              <li key={item.issue}>
                {item.issue}
                {item.source === 'code' && <span className="tag">checked by code</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {analysis.missing_questions?.length > 0 && (
        <div className="notice notice-warn">
          <strong>Missing information</strong>
          <ul>{analysis.missing_questions.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
      )}
    </div>
  )
}

function ChatMessage({ message }) {
  const { response } = message
  const source = response.answer_source
  const sourceLabel = {
    nvidia: `Answered by ${(response.model || 'NVIDIA').split('/').pop()}`,
    verified_comparison: 'Verified comparison',
  }[source]
  const verification = VERIFICATION[response.verification]
  return (
    <div className="turn">
      <div className="bubble bubble-user">{message.question}</div>
      <div className={`bubble bubble-bot ${response.comparison ? 'bubble-wide' : ''}`}>
        {response.comparison ? <ComparisonResult comparison={response.comparison} /> : <div className="answer">{response.answer}</div>}
        {response.explanation && <p className="explanation">{response.explanation}</p>}

        {!response.comparison && response.citations?.length > 0 && (
          <div className="citations">
            {response.citations.map((citation, index) => (
              <blockquote key={`${citation.document}-${index}`} title={citation.quote}>
                {formatQuote(citation.quote)}
                <cite>{citation.document}{citation.lines?.length > 0 && ` · ${formatLines(citation.lines)}`}</cite>
              </blockquote>
            ))}
          </div>
        )}

        <div className="meta">
          <span className="badge badge-muted">{sourceLabel}</span>
          {verification && <span className={`badge ${verification.tone}`}>{verification.label}</span>}
          {response.verification_note && response.verification !== 'verified' && (
            <span className="meta-note">{response.verification_note}</span>
          )}
        </div>
      </div>
    </div>
  )
}

function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(() => {
    return localStorage.getItem('docwise_auth') === 'true'
  })
  const [health, setHealth] = useState(null)
  const [documents, setDocuments] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [view, setView] = useState('analysis')
  const [analyses, setAnalyses] = useState({})
  const [analyzingIds, setAnalyzingIds] = useState([])
  const [comparison, setComparison] = useState(null)
  const [comparing, setComparing] = useState(false)
  const [docError, setDocError] = useState('')
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [pasteOpen, setPasteOpen] = useState(false)
  const [pasteName, setPasteName] = useState('')
  const [pasteText, setPasteText] = useState('')
  const [question, setQuestion] = useState('')
  const [chatMessages, setChatMessages] = useState([])
  const [chatError, setChatError] = useState('')
  const [chatting, setChatting] = useState(false)
  const fileInput = useRef(null)
  const chatEnd = useRef(null)

  const selected = documents.find((doc) => doc.id === selectedId)
  const hasDocuments = documents.length > 0
  const suggestions = documents.length > 1 ? MULTI_DOC_QUESTIONS : SINGLE_DOC_QUESTIONS

  useEffect(() => {
    fetch(`${API_BASE}/api/health`).then((r) => r.json()).then(setHealth).catch(() => setHealth(null))
  }, [])

  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [chatMessages, chatting])

  const addDocuments = (newDocs) => {
    if (!newDocs.length) return
    setDocuments((docs) => [...docs, ...newDocs])
    setSelectedId(newDocs[0].id)
    setView('analysis')
    setComparison(null)
  }

  const loadFiles = async (fileList) => {
    const files = Array.from(fileList || [])
    if (!files.length) return
    setUploading(true)
    setDocError('')
    try {
      addDocuments(await Promise.all(files.map(fileToDoc)))
    } catch (error) {
      setDocError(error.message)
    } finally {
      setUploading(false)
    }
  }

  const loadSamples = async () => {
    setDocError('')
    try {
      const response = await fetch(`${API_BASE}/api/samples`)
      const data = await response.json()
      addDocuments(data.documents.map((doc) => makeDoc(doc.name, doc.text)))
    } catch {
      setDocError('Could not load the sample documents from the backend.')
    }
  }

  const addPasted = () => {
    if (!pasteText.trim()) return
    addDocuments([makeDoc(pasteName.trim() || `pasted_${documents.length + 1}.txt`, pasteText)])
    setPasteName('')
    setPasteText('')
    setPasteOpen(false)
  }

  const removeDocument = (id) => {
    setDocuments((docs) => {
      const remaining = docs.filter((doc) => doc.id !== id)
      if (id === selectedId) setSelectedId(remaining[0]?.id ?? null)
      return remaining
    })
    setAnalyses(({ [id]: _removed, ...rest }) => rest)
    setComparison(null)
  }

  const clearAll = () => {
    setDocuments([])
    setSelectedId(null)
    setAnalyses({})
    setComparison(null)
    setChatMessages([])
    setDocError('')
    setChatError('')
  }

  const analyze = async (docs) => {
    const pending = docs.filter((doc) => !analyzingIds.includes(doc.id))
    if (!pending.length) return
    setDocError('')
    setAnalyzingIds((ids) => [...ids, ...pending.map((doc) => doc.id)])
    await Promise.all(pending.map(async (doc) => {
      try {
        const result = await postJson('/api/analyze', { name: doc.name, text: doc.text })
        setAnalyses((current) => ({ ...current, [doc.id]: result }))
      } catch (error) {
        setDocError(`${doc.name}: ${error.message}`)
      } finally {
        setAnalyzingIds((ids) => ids.filter((id) => id !== doc.id))
      }
    }))
  }

  const compare = async () => {
    setComparing(true)
    setDocError('')
    try {
      setComparison(await postJson('/api/compare', { documents: documents.map(({ name, text }) => ({ name, text })) }))
    } catch (error) {
      setDocError(error.message)
    } finally {
      setComparing(false)
    }
  }

  const askQuestion = async (text = question) => {
    const submittedQuestion = text.trim()
    if (!submittedQuestion || !hasDocuments || chatting) return
    setChatting(true)
    setChatError('')
    setQuestion('')
    try {
      const data = await postJson('/api/answer', {
        question: submittedQuestion,
        documents: documents.map(({ name, text: body }) => ({ name, text: body })),
      })
      setChatMessages((messages) => [...messages, { question: submittedQuestion, response: data }])
    } catch (error) {
      setChatError(error.message)
      setQuestion(submittedQuestion)
    } finally {
      setChatting(false)
    }
  }

  const dropProps = {
    onDragOver: (event) => { event.preventDefault(); setDragging(true) },
    onDragLeave: () => setDragging(false),
    onDrop: (event) => { event.preventDefault(); setDragging(false); loadFiles(event.dataTransfer.files) },
  }

  const handleLogin = () => {
    localStorage.setItem('docwise_auth', 'true')
    setIsAuthenticated(true)
  }

  const handleLogout = () => {
    localStorage.removeItem('docwise_auth')
    setIsAuthenticated(false)
  }

  if (!isAuthenticated) {
    return <Login onLogin={handleLogin} />
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="logo" aria-hidden="true">DA</div>
          <div>
            <h1>Document Analyst</h1>
            <p className="subtitle">Classify, extract, cross-check and question invoices, POs, emails, policies and delivery notes</p>
          </div>
        </div>
        <div className="user-nav">
          <ModelBadge health={health} />
          <span className="user-badge"><span className="user-dot" /> admin</span>
          <button type="button" className="btn btn-ghost" onClick={handleLogout}>Log out</button>
        </div>
      </header>

      <input ref={fileInput} type="file" hidden multiple accept=".txt,.md,.eml,.html,.pdf"
        onChange={(event) => { loadFiles(event.target.files); event.target.value = '' }} />

      <main className="layout">
        <div className="column">
          {/* ---------- Document set ---------- */}
          <section className="card" {...dropProps}>
            <div className="card-header">
              <h2>Documents {hasDocuments && <span className="count">{documents.length}</span>}</h2>
              {hasDocuments && <button type="button" className="btn btn-ghost" onClick={clearAll}>Clear all</button>}
            </div>

            {!hasDocuments ? (
              <button type="button" className={`dropzone ${dragging ? 'dragging' : ''}`} onClick={() => fileInput.current?.click()} disabled={uploading}>
                <span className="dropzone-icon" aria-hidden="true">↑</span>
                <strong>{uploading ? 'Reading files…' : 'Drop files here or click to upload'}</strong>
                <span>Add several at once, e.g. an invoice and its purchase order · PDF, TXT, MD, EML, HTML</span>
              </button>
            ) : (
              <ul className={`doc-list ${dragging ? 'dragging' : ''}`}>
                {documents.map((doc) => {
                  const analysis = analyses[doc.id]
                  return (
                    <li key={doc.id}>
                      <button type="button" className={`doc-row ${doc.id === selectedId ? 'active' : ''}`} onClick={() => setSelectedId(doc.id)}>
                        <span className="file-icon" aria-hidden="true">{fileExt(doc.name)}</span>
                        <span className="doc-name">{doc.name}</span>
                        {analyzingIds.includes(doc.id) ? <span className="spinner small" /> : analysis && (
                          <span className="badge badge-accent">{formatLabel(analysis.document_type)}</span>
                        )}
                      </button>
                      <button type="button" className="icon-btn" aria-label={`Remove ${doc.name}`} onClick={() => removeDocument(doc.id)}>×</button>
                    </li>
                  )
                })}
              </ul>
            )}

            {pasteOpen && (
              <div className="paste-box">
                <input value={pasteName} onChange={(e) => setPasteName(e.target.value)} placeholder="Name (optional), e.g. po_778.txt" />
                <textarea value={pasteText} onChange={(e) => setPasteText(e.target.value)} rows={6} placeholder="Paste the document text…" />
                <div className="row-end">
                  <button type="button" className="btn btn-ghost" onClick={() => setPasteOpen(false)}>Cancel</button>
                  <button type="button" className="btn btn-primary" onClick={addPasted} disabled={!pasteText.trim()}>Add document</button>
                </div>
              </div>
            )}

            <div className="actions">
              <div className="action-links">
                {hasDocuments && <button type="button" className="btn btn-link" onClick={() => fileInput.current?.click()}>+ Upload</button>}
                <button type="button" className="btn btn-link" onClick={() => setPasteOpen(true)}>+ Paste text</button>
                <button type="button" className="btn btn-link" onClick={loadSamples}>Load sample set</button>
              </div>
              {hasDocuments && (
                <button type="button" className="btn btn-primary" onClick={() => analyze(documents)} disabled={analyzingIds.length > 0}>
                  {analyzingIds.length ? <><span className="spinner" /> Analyzing…</> : `Analyze ${documents.length > 1 ? 'all' : ''}`}
                </button>
              )}
            </div>

            {docError && <div className="notice notice-error">{docError}</div>}
          </section>

          {/* ---------- Cross-document check ---------- */}
          {documents.length > 1 && (
            <section className="card">
              <div className="card-header">
                <h2>Invoice vs purchase order</h2>
                <button type="button" className="btn btn-primary" onClick={compare} disabled={comparing}>
                  {comparing ? <><span className="spinner" /> Checking…</> : comparison ? 'Re-check' : 'Run check'}
                </button>
              </div>
              {comparison ? <ComparisonResult comparison={comparison} /> : (
                <p className="muted">Finds the invoice and purchase order in this set, verifies totals, vendor and PO reference, and flags any discrepancy.</p>
              )}
            </section>
          )}

          {/* ---------- Selected document ---------- */}
          {selected && (
            <section className="card">
              <div className="card-header">
                <h2 className="truncate">{selected.name}</h2>
                <div className="tabs" role="tablist">
                  {['analysis', 'text'].map((tab) => (
                    <button key={tab} type="button" role="tab" aria-selected={view === tab} className={`tab ${view === tab ? 'active' : ''}`} onClick={() => setView(tab)}>
                      {tab === 'analysis' ? 'Analysis' : 'Text'}
                    </button>
                  ))}
                </div>
              </div>
              {view === 'text' ? (
                <pre className="doc-text">{selected.text}</pre>
              ) : analyses[selected.id] ? (
                <AnalysisView analysis={analyses[selected.id]} />
              ) : (
                <div className="empty-inline">
                  <p className="muted">Not analyzed yet.</p>
                  <button type="button" className="btn btn-ghost" onClick={() => analyze([selected])} disabled={analyzingIds.includes(selected.id)}>
                    {analyzingIds.includes(selected.id) ? 'Analyzing…' : 'Analyze this document'}
                  </button>
                </div>
              )}
            </section>
          )}
        </div>

        {/* ---------- Chat ---------- */}
        <section className="card chat">
          <div className="card-header">
            <h2>Ask about {documents.length > 1 ? 'these documents' : 'this document'}</h2>
            {hasDocuments && <span className="badge badge-muted">{documents.length} in context</span>}
          </div>

          <div className="chat-scroll" aria-live="polite">
            {chatMessages.length === 0 && !chatting && (
              <div className="empty">
                <p>{hasDocuments ? 'Try one of these:' : 'Add documents to start asking questions.'}</p>
                {hasDocuments && (
                  <div className="chips">
                    {suggestions.map((item) => (
                      <button type="button" key={item} className="chip" onClick={() => askQuestion(item)}>{item}</button>
                    ))}
                  </div>
                )}
              </div>
            )}

            {chatMessages.map((message, index) => (
              <ChatMessage key={`${index}-${message.question}`} message={message} />
            ))}

            {chatting && (
              <div className="bubble bubble-bot thinking">
                <span className="typing"><i /><i /><i /></span> Reading the documents and reasoning…
              </div>
            )}
            <div ref={chatEnd} />
          </div>

          {chatError && <div className="notice notice-error">{chatError}</div>}

          <form className="composer" onSubmit={(event) => { event.preventDefault(); askQuestion() }}>
            <textarea
              value={question}
              rows={1}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  askQuestion()
                }
              }}
              placeholder={hasDocuments ? 'Ask about totals, tax, dates, discrepancies…' : 'Add a document first'}
              disabled={!hasDocuments}
            />
            <button type="submit" className="btn btn-primary send" disabled={chatting || !hasDocuments || !question.trim()} aria-label="Send">➤</button>
          </form>
        </section>
      </main>
    </div>
  )
}

export default App
