// @vitest-environment jsdom
// --- VaultHealthPanel.test.jsx ---
// Landing-page "Vault repair health" panel: fetches /api/vault/repair/status,
// groups findings into a fixed five-section risk order, gates every repair
// action behind a mandatory preview dialog, and never surfaces issue_category.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'

vi.mock('../../globalApi', () => ({
  fetchVaultRepairStatus: vi.fn(),
  fetchVaultRepairApply: vi.fn(),
}))

import { fetchVaultRepairStatus, fetchVaultRepairApply } from '../../globalApi'
import VaultHealthPanel from '../VaultHealthPanel'

// All nine contract keys for a finding, with sensible defaults.
function makeFinding(overrides = {}) {
  return {
    id: 'f1',
    category: 'permission_integrity',
    issue_category: 'unknown_top_key',
    severity: 'warning',
    file: 'vault/notes.md',
    message: 'a finding message',
    suggested_fix: 'apply the suggested fix',
    classification: 'machine_apply',
    path_in_file: 'collections.notes',
    ...overrides,
  }
}

function makeStatus(overrides = {}) {
  return {
    findings: [],
    repairs_available: false,
    summary: { total: 0, machine_appliable: 0, manual_review: 0 },
    extra_files: [],
    seeded_files: [],
    run: { started_at: '2024-01-01T00:00:00Z', finished_at: '2024-01-01T00:00:01Z' },
    issues: [],
    ...overrides,
  }
}

async function renderPanel(status, defaultOpen = false) {
  fetchVaultRepairStatus.mockResolvedValue(status)
  const view = render(<VaultHealthPanel defaultOpen={defaultOpen} />)
  await waitFor(() => {
    expect(view.container.querySelector('.vault-health-panel')).not.toBeNull()
  })
  return view
}

async function openPreviewViaSelectAll(container) {
  fireEvent.click(container.querySelector('.vault-health-panel-select-all input'))
  fireEvent.click(screen.getByRole('button', { name: /Apply selected repairs/ }))
  return screen.findByRole('dialog')
}

function groupKeys(container) {
  return Array.from(container.querySelectorAll('.vault-health-panel-group')).map((group) => {
    if (group.classList.contains('vault-health-panel-group-security-critical')) return 'security_critical'
    if (group.classList.contains('vault-health-panel-group-permission-integrity')) return 'permission_integrity'
    if (group.classList.contains('vault-health-panel-group-config-drift')) return 'config_drift'
    if (group.classList.contains('vault-health-panel-group-cosmetic')) return 'cosmetic'
    if (group.classList.contains('vault-health-panel-group-other')) return 'other'
    return 'unknown'
  })
}

beforeEach(() => {
  fetchVaultRepairStatus.mockReset()
  fetchVaultRepairApply.mockReset()
  fetchVaultRepairApply.mockResolvedValue({ backups_created: 0, quarantine_moves: [] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('VaultHealthPanel', () => {
  it('groups findings into the fixed risk order with the Other bucket last and visible', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 's', category: 'security_critical', severity: 'error', message: 'sec finding' }),
        makeFinding({ id: 'p', category: 'permission_integrity', message: 'perm finding' }),
        makeFinding({ id: 'c', category: 'config_drift', message: 'config finding' }),
        makeFinding({ id: 'k', category: 'cosmetic', message: 'cosmetic finding' }),
        makeFinding({ id: 'o', category: 'mystery_zone', message: 'mystery finding visible' }),
      ],
    })
    const { container } = await renderPanel(status, true)
    expect(groupKeys(container)).toEqual([
      'security_critical',
      'permission_integrity',
      'config_drift',
      'cosmetic',
      'other',
    ])
    expect(screen.getByText('mystery finding visible')).toBeVisible()
  })

  it('maps risk and severity to their colour classes', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 'p', category: 'permission_integrity', severity: 'error', message: 'perm colour' }),
        makeFinding({ id: 'c', category: 'config_drift', severity: 'warning', message: 'config colour' }),
      ],
    })
    const { container } = await renderPanel(status, true)

    const permItem = screen.getByText('perm colour').closest('li')
    expect(container.querySelector('.vault-health-panel-group-permission-integrity')).not.toBeNull()
    expect(permItem).toHaveClass('vault-health-panel-item-permission-integrity')
    expect(permItem.querySelector('.vault-health-panel-sev')).toHaveClass('vault-health-panel-sev-error')

    const configItem = screen.getByText('config colour').closest('li')
    expect(container.querySelector('.vault-health-panel-group-config-drift')).not.toBeNull()
    expect(configItem).toHaveClass('vault-health-panel-item-config-drift')
    expect(configItem.querySelector('.vault-health-panel-sev')).toHaveClass('vault-health-panel-sev-warning')
  })

  it('offers a bulk select-all control covering the bulk-safe machine_apply findings', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 'p', category: 'permission_integrity' }),
        makeFinding({ id: 'c', category: 'config_drift' }),
      ],
    })
    const { container } = await renderPanel(status, true)
    expect(screen.getByText(/Select all machine-appliable fixes/)).toHaveTextContent('(2)')
    expect(
      container.querySelectorAll('.vault-health-panel-select input[type="checkbox"]')
    ).toHaveLength(2)
  })

  it('gives security_critical findings a per-item action but no checkbox or bulk selection', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [makeFinding({ id: 's', category: 'security_critical', severity: 'error' })],
    })
    const { container } = await renderPanel(status, true)
    const item = container.querySelector('.vault-health-panel-item-security-critical')
    expect(item).not.toBeNull()
    expect(item.querySelector('input[type="checkbox"]')).toBeNull()
    expect(item.querySelector('.vault-health-panel-item-action')).not.toBeNull()
    expect(container.querySelector('.vault-health-panel-select-all')).toBeNull()
  })

  it('gives cosmetic and manual_review findings no checkbox and no per-item apply action', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 'k', category: 'cosmetic', classification: 'machine_apply', message: 'cosmetic finding' }),
        makeFinding({ id: 'm', category: 'permission_integrity', classification: 'manual_review', message: 'manual finding' }),
      ],
    })
    await renderPanel(status, true)
    const cosmetic = screen.getByText('cosmetic finding').closest('li')
    const manual = screen.getByText('manual finding').closest('li')
    expect(cosmetic.querySelector('input[type="checkbox"]')).toBeNull()
    expect(cosmetic.querySelector('.vault-health-panel-item-action')).toBeNull()
    expect(manual.querySelector('input[type="checkbox"]')).toBeNull()
    expect(manual.querySelector('.vault-health-panel-item-action')).toBeNull()
  })

  it('renders the unknown/Other finding but offers no apply affordance', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [makeFinding({ id: 'o', category: 'mystery_zone', message: 'unknown finding' })],
    })
    const { container } = await renderPanel(status, true)
    const item = screen.getByText('unknown finding').closest('li')
    expect(item).toHaveClass('vault-health-panel-item-other')
    expect(item.querySelector('input[type="checkbox"]')).toBeNull()
    expect(item.querySelector('.vault-health-panel-item-action')).toBeNull()
    expect(container.querySelector('.vault-health-panel-apply')).toBeNull()
  })

  it('opens the preview dialog for bulk apply without calling the apply API', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 'p1', category: 'permission_integrity', file: 'vault/perm.md', path_in_file: 'collections.perm', message: 'perm problem', suggested_fix: 'fix perm' }),
        makeFinding({ id: 'c1', category: 'config_drift', file: 'vault/config.md', path_in_file: 'collections.config', message: 'config problem', suggested_fix: 'fix config' }),
      ],
    })
    const { container } = await renderPanel(status, true)
    const dialog = await openPreviewViaSelectAll(container)
    expect(fetchVaultRepairApply).not.toHaveBeenCalled()
    expect(dialog).toHaveTextContent('Apply 2 selected repairs')
    expect(dialog).toHaveTextContent('vault/perm.md')
    expect(dialog).toHaveTextContent('collections.perm')
    expect(dialog).toHaveTextContent('fix perm')
    expect(dialog).toHaveTextContent('perm problem')
    expect(dialog).toHaveTextContent('vault/config.md')
    expect(dialog).toHaveTextContent('collections.config')
    expect(dialog).toHaveTextContent('fix config')
    expect(dialog).toHaveTextContent('config problem')
  })

  it('opens the same dialog scoped to a single finding from the per-item action', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 's1', category: 'security_critical', severity: 'error', file: 'vault/sec.md', message: 'sec problem' }),
      ],
    })
    await renderPanel(status, true)
    fireEvent.click(screen.getByRole('button', { name: /Preview fix for vault\/sec\.md/ }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Apply 1 selected repair')
    expect(dialog).toHaveTextContent('vault/sec.md')
    expect(fetchVaultRepairApply).not.toHaveBeenCalled()
  })

  it('cancelling the dialog closes it and performs no apply', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 'p1', category: 'permission_integrity' }),
        makeFinding({ id: 'c1', category: 'config_drift' }),
      ],
    })
    const { container } = await renderPanel(status, true)
    await openPreviewViaSelectAll(container)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(fetchVaultRepairApply).not.toHaveBeenCalled()
  })

  it('renders findings with an informational hint and no apply affordances when repairs are unavailable', async () => {
    const status = makeStatus({
      repairs_available: false,
      findings: [
        makeFinding({ id: 'p', category: 'permission_integrity', message: 'perm finding' }),
        makeFinding({ id: 'c', category: 'config_drift', message: 'config finding' }),
      ],
    })
    const { container } = await renderPanel(status, true)
    expect(screen.getByText('perm finding')).toBeInTheDocument()
    expect(screen.getByText('config finding')).toBeInTheDocument()
    expect(screen.getByText(/No automatic repairs are available/)).toBeInTheDocument()
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(0)
    expect(container.querySelector('.vault-health-panel-item-action')).toBeNull()
    expect(container.querySelector('.vault-health-panel-apply')).toBeNull()
  })

  it('renders the apply affordances when repairs are available', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 'p', category: 'permission_integrity' }),
        makeFinding({ id: 'c', category: 'config_drift' }),
      ],
    })
    const { container } = await renderPanel(status, true)
    expect(container.querySelector('.vault-health-panel-select-all input[type="checkbox"]')).not.toBeNull()
    expect(container.querySelector('.vault-health-panel-apply')).not.toBeNull()
    expect(
      container.querySelectorAll('.vault-health-panel-select input[type="checkbox"]')
    ).toHaveLength(2)
  })

  it('shows the healthy state with a working Re-scan button even when collapsed by default', async () => {
    const first = await renderPanel(makeStatus({ repairs_available: false }), false)
    expect(screen.getByText('Vault is healthy \u2014 no issues found.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Re-scan' }))
    await waitFor(() => expect(fetchVaultRepairStatus).toHaveBeenCalledTimes(2))
    first.unmount()

    fetchVaultRepairStatus.mockClear()
    await renderPanel(makeStatus({ repairs_available: true }), false)
    expect(screen.getByText('Vault is healthy \u2014 no issues found.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Re-scan' }))
    await waitFor(() => expect(fetchVaultRepairStatus).toHaveBeenCalledTimes(2))
  })

  it('renders a distinct unavailable state with a Retry control that re-fetches', async () => {
    const { container } = await renderPanel(null, false)
    expect(container.querySelector('.vault-health-panel-unavailable')).not.toBeNull()
    expect(screen.queryByText(/Vault is healthy/)).toBeNull()
    expect(container.querySelector('.vault-health-panel-verdict-ok')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(fetchVaultRepairStatus).toHaveBeenCalledTimes(2))
  })

  it('applies the exact selected ids after confirmation, shows success and re-fetches', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 'p1', category: 'permission_integrity', message: 'perm finding' }),
        makeFinding({ id: 'c1', category: 'config_drift', message: 'config finding' }),
      ],
    })
    fetchVaultRepairApply.mockResolvedValue({ backups_created: 2, quarantine_moves: ['x', 'y'] })
    const { container } = await renderPanel(status, true)
    await openPreviewViaSelectAll(container)
    fireEvent.click(screen.getByRole('button', { name: 'Apply repairs' }))
    await waitFor(() =>
      expect(fetchVaultRepairApply).toHaveBeenCalledWith({ repair_ids: ['p1', 'c1'], confirmed: true })
    )
    expect(await screen.findByText(/Repairs applied\./)).toBeInTheDocument()
    await waitFor(() => expect(fetchVaultRepairStatus).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('shows an error, keeps the dialog closed and preserves the selection when apply fails', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 'p1', category: 'permission_integrity' }),
        makeFinding({ id: 'c1', category: 'config_drift' }),
      ],
    })
    fetchVaultRepairApply.mockResolvedValue(null)
    const { container } = await renderPanel(status, true)
    await openPreviewViaSelectAll(container)
    fireEvent.click(screen.getByRole('button', { name: 'Apply repairs' }))
    expect(await screen.findByText(/Could not apply repairs/)).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(container.querySelector('.vault-health-panel-select-all input').checked).toBe(true)
    const boxes = container.querySelectorAll('.vault-health-panel-select input')
    expect(boxes).toHaveLength(2)
    expect(Array.from(boxes).every((box) => box.checked)).toBe(true)
  })

  it('never leaks internal fields into the rendered text', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [
        makeFinding({ id: 'p1', category: 'permission_integrity', severity: 'error', message: 'perm message', suggested_fix: 'perm fix' }),
        makeFinding({ id: 'o1', category: 'mystery_zone', classification: 'manual_review', severity: 'info', message: 'mystery message', suggested_fix: 'mystery fix' }),
      ],
    })
    const { container } = await renderPanel(status, true)
    const text = container.textContent
    expect(text).not.toContain('unknown_top_key')
    expect(text).not.toContain('issue_category')
    expect(text).not.toContain('undefined')
    expect(text).not.toContain('null')
  })

  it('keeps the findings body unmounted until the toggle is clicked', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [makeFinding({ id: 'p1', category: 'permission_integrity', message: 'perm finding' })],
    })
    const { container } = await renderPanel(status, false)
    expect(container.querySelector('#vault-health-panel-body')).toBeNull()
    expect(container.querySelector('.vault-health-panel-group')).toBeNull()
    const toggle = screen.getByRole('button', { name: /Expand details/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(container.querySelector('#vault-health-panel-body')).not.toBeNull()
    expect(container.querySelector('.vault-health-panel-group')).not.toBeNull()
  })

  it('renders without any inline styles', async () => {
    const status = makeStatus({
      repairs_available: true,
      findings: [makeFinding({ id: 'p1', category: 'permission_integrity', message: 'perm finding' })],
    })
    const { container } = await renderPanel(status, true)
    expect(container.querySelectorAll('[style]')).toHaveLength(0)
  })
})
