// --- modals/ContainerLogsModal.jsx ---
// Phase 4: dedicated placeholder for the Containers tab "Logs" button.
// Reuses the generic PlaceholderModal so the modal markup stays in one place.

import React from 'react'
import { PlaceholderModal } from '../workspaceUtils.jsx'

export default function ContainerLogsModal({ onClose }) {
  return <PlaceholderModal title="Container logs" message="Live container logs are coming soon." onClose={onClose} />
}
