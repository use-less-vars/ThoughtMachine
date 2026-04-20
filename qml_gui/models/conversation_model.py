"""
Conversation model for QML GUI.

Exposes the session's user_history as a QAbstractListModel with roles for display.
"""
import html
from enum import IntEnum
from PyQt6.QtCore import QAbstractListModel, QModelIndex, Qt, pyqtSlot, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from agent.logging import log
try:
    from qt_gui.panels.markdown_renderer import MarkdownRenderer
    HAS_MARKDOWN_RENDERER = True
except ImportError:
    HAS_MARKDOWN_RENDERER = False

    class SimpleMarkdownRenderer:
        """Simple fallback that converts markdown to basic HTML."""

        def markdown_to_html(self, markdown_text: str) -> str:
            import html
            escaped = html.escape(markdown_text)
            return f'<pre>{escaped}</pre>'
    MarkdownRenderer = SimpleMarkdownRenderer

class ConversationRole(IntEnum):
    """Roles for conversation model."""
    RoleRole = Qt.ItemDataRole.UserRole + 1
    RoleContent = Qt.ItemDataRole.UserRole + 2
    RoleHtmlContent = Qt.ItemDataRole.UserRole + 3
    RoleToolName = Qt.ItemDataRole.UserRole + 4
    RoleIsFinal = Qt.ItemDataRole.UserRole + 5
    RoleReasoning = Qt.ItemDataRole.UserRole + 6
    RoleToolCalls = Qt.ItemDataRole.UserRole + 7
    RoleToolCallId = Qt.ItemDataRole.UserRole + 8
    RoleCreatedAt = Qt.ItemDataRole.UserRole + 9
    RoleSeq = Qt.ItemDataRole.UserRole + 10

class ConversationModel(QAbstractListModel):
    """Model that exposes session's user_history to QML."""
    conversationReset = pyqtSignal()

    def __init__(self, presenter=None, parent=None):
        super().__init__(parent)
        self.presenter = presenter
        self._user_history = []
        self.markdown_renderer = MarkdownRenderer()
        if presenter:
            self._connect_presenter()
            self._refresh_history()

    def _connect_presenter(self):
        """Connect to presenter signals."""
        log('DEBUG', 'gui.conversation_model', f'_connect_presenter: presenter={self.presenter}')
        if hasattr(self.presenter, 'conversation_changed'):
            log('DEBUG', 'gui.conversation_model', f'_connect_presenter: connecting to conversation_changed')
            self.presenter.conversation_changed.connect(self._on_conversation_changed)
            log('DEBUG', 'gui.conversation_model', f'_connect_presenter: connected to conversation_changed')
        else:
            log('DEBUG', 'gui.conversation_model', f'_connect_presenter: presenter has no conversation_changed attribute')

    @pyqtSlot()
    def _on_conversation_changed(self):
        """Called when conversation changes; trigger model reset."""
        log('DEBUG', 'gui.conversation_model', f'_on_conversation_changed, history length: {(len(self._user_history) if self._user_history else 0)}')
        self.beginResetModel()
        self._refresh_history()
        self.endResetModel()
        self.conversationReset.emit()

    def _refresh_history(self):
        """Refresh internal copy of user_history from presenter."""
        log('DEBUG', 'gui.conversation_model', '_refresh_history called')
        if not self.presenter:
            self._user_history = []
            log('DEBUG', 'gui.conversation_model', f'_refresh_history: no presenter, history cleared')
            return
        try:
            if hasattr(self.presenter, 'state_bridge') and self.presenter.state_bridge:
                history = self.presenter.state_bridge.user_history
                log('DEBUG', 'gui.conversation_model', f'_refresh_history: raw history from state_bridge: {history}')
                if history is not None:
                    self._user_history = list(history)
                    log('DEBUG', 'gui.conversation_model', f'_refresh_history: copied history, length: {len(self._user_history)}')
                else:
                    self._user_history = []
                    log('DEBUG', 'gui.conversation_model', f'_refresh_history: history is None')
            else:
                self._user_history = []
                log('DEBUG', 'gui.conversation_model', f'_refresh_history: no state_bridge attribute')
        except Exception as e:
            log('ERROR', 'gui.conversation_model', f'Error refreshing history: {e}')
            self._user_history = []

    def rowCount(self, parent=QModelIndex()):
        return len(self._user_history)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._user_history):
            return None
        message = self._user_history[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            role_str = message.get('role', 'unknown')
            content = message.get('content', '')
            snippet = content[:50] + ('...' if len(content) > 50 else '')
            return f'[{role_str}] {snippet}'
        if role == ConversationRole.RoleRole:
            return message.get('role', '')
        elif role == ConversationRole.RoleContent:
            return message.get('content', '')
        elif role == ConversationRole.RoleHtmlContent:
            content = message.get('content', '')
            if content:
                return self.markdown_renderer.markdown_to_html(content)
            return ''
        elif role == ConversationRole.RoleToolName:
            return message.get('tool_name', '')
        elif role == ConversationRole.RoleIsFinal:
            tool_name = message.get('tool_name', '')
            return tool_name == 'Final'
        elif role == ConversationRole.RoleReasoning:
            return message.get('reasoning', '')
        elif role == ConversationRole.RoleToolCalls:
            return message.get('tool_calls', [])
        elif role == ConversationRole.RoleToolCallId:
            return message.get('tool_call_id', '')
        elif role == ConversationRole.RoleCreatedAt:
            return message.get('created_at', '')
        elif role == ConversationRole.RoleSeq:
            return message.get('seq', -1)
        return None

    def roleNames(self):
        """Return mapping from role enum to role names for QML."""
        return {ConversationRole.RoleRole: b'role', ConversationRole.RoleContent: b'content', ConversationRole.RoleHtmlContent: b'htmlContent', ConversationRole.RoleToolName: b'toolName', ConversationRole.RoleIsFinal: b'isFinal', ConversationRole.RoleReasoning: b'reasoning', ConversationRole.RoleToolCalls: b'toolCalls', ConversationRole.RoleToolCallId: b'toolCallId', ConversationRole.RoleCreatedAt: b'createdAt', ConversationRole.RoleSeq: b'seq'}

    def setPresenter(self, presenter):
        """Set the presenter and connect signals."""
        if self.presenter:
            pass
        self.presenter = presenter
        self._connect_presenter()
        self._refresh_history()
        self.beginResetModel()
        self.endResetModel()