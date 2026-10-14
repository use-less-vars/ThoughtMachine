// @vitest-environment jsdom
/*
 * WorkerFormModal.fieldNameMismatch.test.jsx — regression guard for the
 * worker create/edit modal silently dropping fields.
 *
 * Bug: the modal posted NON-canonical keys that its schema
 * (agent/models/worker_definition.py :: WorkerDefinition, extra='ignore')
 * does not recognise, so the values were silently discarded on save:
 *
 *   form posted `max_context_tokens` -> canonical field `max_tokens` -> dropped
 *   form posted `turn_limit`         -> canonical field `max_turns`  -> dropped
 *
 * The fix makes the form post (and read back) the canonical WorkerDefinition
 * field names. This test FAILS before the fix (the payload carries the dropped
 * keys and the edit form fails to re-hydrate) and PASSES after it.
 */

import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
// The modal is co-located in WorkerManagementPanel.jsx (no standalone file).
import { WorkerFormModal } from '../WorkerManagementPanel'

// Locate the <input>/<textarea> belonging to the field whose <label> text is
// exactly `labelText`. The modal renders each field as
// <div><label>…</label><input/></div>, so the control is a sibling of the
// label inside the label's parent element. Labels in this modal are not wired
// via htmlFor/id, so getByLabelText cannot be used.
function controlForLabel(labelText) {
  const label = screen.getByText(labelText)
  const control = label.parentElement.querySelector('input, textarea')
  if (!control) throw new Error(`no form control for label "${labelText}"`)
  return control
}

function renderModal(props = {}) {
  const onSave = vi.fn()
  render(
    <WorkerFormModal
      worker={null}
      templates={[]}
      onSave={onSave}
      onCancel={vi.fn()}
      isCreateFromTemplate={false}
      {...props}
    />
  )
  return onSave
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('WorkerFormModal — canonical WorkerDefinition field names', () => {
  it('posts max_turns / max_tokens (NOT turn_limit / max_context_tokens)', () => {
    const onSave = renderModal()

    fireEvent.change(controlForLabel('Name *'), { target: { value: 'my-coder' } })
    fireEvent.change(controlForLabel('Max Tokens'), { target: { value: '123' } })
    fireEvent.change(controlForLabel('Turns'), { target: { value: '5' } })

    fireEvent.click(screen.getByRole('button', { name: 'Create Worker' }))

    expect(onSave).toHaveBeenCalledTimes(1)
    const [payload] = onSave.mock.calls[0]

    // Canonical keys carry the entered values …
    expect(payload.max_tokens).toBe(123)
    expect(payload.max_turns).toBe(5)
    // … and the non-canonical keys the schema ignores must NOT be posted.
    expect(payload).not.toHaveProperty('max_context_tokens')
    expect(payload).not.toHaveProperty('turn_limit')
  })

  it('re-hydrates canonical fields when editing (round-trip)', () => {
    const onSave = renderModal({
      worker: { name: 'w', max_tokens: 4096, max_turns: 7 },
    })

    // The form must show the canonical values that were persisted.
    expect(controlForLabel('Max Tokens').value).toBe('4096')
    expect(controlForLabel('Turns').value).toBe('7')

    fireEvent.click(screen.getByRole('button', { name: 'Update Worker' }))

    const [payload] = onSave.mock.calls[0]
    expect(payload.max_tokens).toBe(4096)
    expect(payload.max_turns).toBe(7)
  })
})
