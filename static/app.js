const analyzeBtn = document.getElementById('analyze-btn');
const compareBtn = document.getElementById('compare-btn');
const answerBtn = document.getElementById('answer-btn');

function setResult(elementId, message, tone = 'success') {
  const box = document.getElementById(elementId);
  box.textContent = message;
  box.classList.remove('empty', 'success', 'warning');
  box.classList.add(tone);
}

function setError(elementId, message) {
  setResult(elementId, message, 'warning');
}

analyzeBtn.addEventListener('click', async () => {
  const payload = {
    name: document.getElementById('doc-name').value || 'document.txt',
    text: document.getElementById('doc-text').value,
  };

  try {
    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    const data = await response.json();
    if (!response.ok) {
      setError('analysis-result', data.error || 'Could not analyze the document.');
      return;
    }

    const summary = JSON.stringify(data, null, 2);
    setResult('analysis-result', summary, 'success');
  } catch (error) {
    setError('analysis-result', 'Request failed: ' + error.message);
  }
});

compareBtn.addEventListener('click', async () => {
  const payload = {
    invoice_name: 'document_a.txt',
    po_name: 'document_b.txt',
    invoice_text: document.getElementById('invoice-text').value,
    po_text: document.getElementById('po-text').value,
  };

  try {
    const response = await fetch('/api/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    const data = await response.json();
    if (!response.ok) {
      setError('compare-result', data.error || 'Comparison failed.');
      return;
    }

    setResult('compare-result', JSON.stringify(data, null, 2), 'success');
  } catch (error) {
    setError('compare-result', 'Request failed: ' + error.message);
  }
});

answerBtn.addEventListener('click', async () => {
  const question = document.getElementById('question-input').value;
  const rawText = document.getElementById('question-docs').value;

  try {
    const response = await fetch('/api/answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question,
        documents: [{ name: 'document.txt', text: rawText }],
      }),
    });

    const data = await response.json();
    if (!response.ok) {
      setError('question-result', data.error || 'Could not answer the question.');
      return;
    }

    setResult('question-result', JSON.stringify(data, null, 2), 'success');
  } catch (error) {
    setError('question-result', 'Request failed: ' + error.message);
  }
});
