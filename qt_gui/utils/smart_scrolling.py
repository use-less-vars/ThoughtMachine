"""Smart scrolling management for QTextEdit widgets."""
from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from PyQt6.QtWidgets import QTextEdit


class SmartScroller(QObject):
    """Manages auto-scrolling behavior for a QTextEdit.
    
    Features:
    - Auto-scroll to bottom when new content is added
    - Disable auto-scroll when user manually scrolls away
    - Re-enable auto-scroll when user scrolls back to bottom
    - Programmatic scroll support with re-entrancy guard
    """
    
    def __init__(self, text_edit: QTextEdit):
        """Initialize smart scroller for a text edit widget."""
        super().__init__()
        self._text_edit = text_edit
        self._auto_scroll_enabled = True
        self._user_scrolled_away = False
        self._programmatic_scroll = False
        self._previous_max = 0
        self._deferred_scroll_timer = None
        self._pause_count = 0  # Temporarily ignore scrollbar changes during rebuilds
        self._pre_pause_user_scrolled_away = None  # Saved state before pause
        self._scroll_retry_count = 0  # Track scroll retry attempts

        # Connect to scrollbar changes
        scrollbar = self._text_edit.verticalScrollBar()
        scrollbar.valueChanged.connect(self._on_scrollbar_value_changed)
        self._previous_max = scrollbar.maximum()        
    def _on_scrollbar_value_changed(self, value: int):
        """Handle scrollbar value change to detect user scrolling."""
        # Skip if this change was caused by programmatic scrolling or we're ignoring changes
        if self._programmatic_scroll or self._pause_count > 0:
            return
        
        scrollbar = self._text_edit.verticalScrollBar()
        max_val = scrollbar.maximum()
        
        # Check if content grew while user was at bottom
        # If max increased and user was at previous max (bottom), they're still at bottom
        if max_val > self._previous_max and value == self._previous_max and self._previous_max > 0:
            # Content grew, user is still effectively at bottom
            self._user_scrolled_away = False
            print(f"[SmartScroller] content grew: prev_max={self._previous_max}, max={max_val}, value={value}, user_scrolled_away=False")
        else:
            # Normal case: user manually scrolled or content shrank
            # If user is within 10 pixels of bottom, consider them at bottom
            self._user_scrolled_away = value < max_val - 10
            if self._user_scrolled_away:
                print(f"[SmartScroller] user scrolled away: value={value}, max={max_val}, threshold={max_val - 10}")
        
        old_enabled = self._auto_scroll_enabled
        self._auto_scroll_enabled = not self._user_scrolled_away
        self._previous_max = max_val
        
        if old_enabled != self._auto_scroll_enabled:
            print(f"[SmartScroller] auto_scroll changed: {old_enabled} -> {self._auto_scroll_enabled}")
        
    def scroll_to_bottom(self):
        """Scroll to bottom if auto-scroll is enabled."""
        print(f"[SmartScroller] scroll_to_bottom called, auto_scroll_enabled={self._auto_scroll_enabled}, user_scrolled_away={self._user_scrolled_away}")
        if self._auto_scroll_enabled:
            self._do_scroll_to_bottom()
    
    def deferred_scroll_to_bottom(self, delay_ms=100):
        """Schedule a scroll to bottom after a short delay.
        
        Useful when content is being added and the scrollbar maximum
        may not be updated immediately.
        """
        print(f"[SmartScroller] deferred_scroll_to_bottom scheduled with delay {delay_ms}ms")
        # Cancel any pending deferred scroll and clean up previous timer
        if self._deferred_scroll_timer:
            self._deferred_scroll_timer.stop()
            self._deferred_scroll_timer.timeout.disconnect()
            self._deferred_scroll_timer.deleteLater()
            self._deferred_scroll_timer = None

        # Schedule new deferred scroll
        self._deferred_scroll_timer = QTimer(self)
        self._deferred_scroll_timer.setSingleShot(True)
        self._deferred_scroll_timer.timeout.connect(self._on_deferred_scroll_timeout)
        self._deferred_scroll_timer.start(delay_ms)
    
    def _on_deferred_scroll_timeout(self):
        """Handle deferred scroll timeout."""
        print(f"[SmartScroller] _on_deferred_scroll_timeout called, auto_scroll_enabled={self._auto_scroll_enabled}")
        self.scroll_to_bottom()            
    def _do_scroll_to_bottom(self):
        """Programmatically scroll to bottom."""
        print(f"[SmartScroller] _do_scroll_to_bottom, programmatic_scroll=True")
        scrollbar = self._text_edit.verticalScrollBar()
        max_val = scrollbar.maximum()
        current_val = scrollbar.value()
        print(f"[SmartScroller] Before scroll: current={current_val}, max={max_val}, retry_count={self._scroll_retry_count}")
        
        # Skip if already at or near bottom (within 2 pixels)
        # For very small content (max_val < 50), always scroll to ensure we're at bottom
        if max_val < 50:
            print(f"[SmartScroller] Content small (max={max_val}), forcing scroll")
        elif current_val >= max_val - 2:
            print(f"[SmartScroller] Already at bottom, skipping scroll")
            self._previous_max = max_val
            self._scroll_retry_count = 0  # Reset retry count on success
            return
            
        self._programmatic_scroll = True
        scrollbar.setValue(max_val)
        after_val = scrollbar.value()
        self._previous_max = max_val
        self._programmatic_scroll = False
        print(f"[SmartScroller] _do_scroll_to_bottom done, value set to {after_val} (target was {max_val})")
        
        # Check if scroll actually reached bottom (within 5 pixels)
        # If not, retry a few times (content may still be rendering)
        if after_val < max_val - 5 and self._scroll_retry_count < 3:
            self._scroll_retry_count += 1
            print(f"[SmartScroller] Scroll didn't reach bottom, retry {self._scroll_retry_count}/3")
            self.deferred_scroll_to_bottom(delay_ms=50)  # Shorter delay for retry
        else:
            # Success or max retries reached
            self._scroll_retry_count = 0
    def set_auto_scroll_enabled(self, enabled: bool):
        """Enable/disable auto-scrolling."""
        self._auto_scroll_enabled = enabled
        if enabled:
            self.scroll_to_bottom()
            
    def is_auto_scroll_enabled(self) -> bool:
        """Check if auto-scroll is currently enabled."""
        return self._auto_scroll_enabled
    
    def reset_auto_scroll(self):
        """Reset auto-scroll state to enabled (e.g., after loading new content)."""
        self._user_scrolled_away = False
        self._auto_scroll_enabled = True
        self._previous_max = self._text_edit.verticalScrollBar().maximum()
    
    def pause_tracking(self):
        """Temporarily ignore scrollbar changes (e.g., during document rebuild)."""
        # Save state on first pause
        if self._pause_count == 0:
            self._pre_pause_user_scrolled_away = self._user_scrolled_away
        self._pause_count += 1
        print(f"[SmartScroller] Tracking paused, pause_count={self._pause_count}, saved_user_scrolled_away={self._pre_pause_user_scrolled_away}")
    
    def resume_tracking(self):
        """Resume tracking scrollbar changes."""
        if self._pause_count > 0:
            self._pause_count -= 1
        
        # Update current state after resume
        scrollbar = self._text_edit.verticalScrollBar()
        max_val = scrollbar.maximum()
        value = scrollbar.value()
        self._previous_max = max_val
        
        # Only update auto-scroll state when fully resumed (pause_count == 0)
        if self._pause_count == 0:
            # Restore user_scrolled_away from before pause
            if self._pre_pause_user_scrolled_away is not None:
                self._user_scrolled_away = self._pre_pause_user_scrolled_away
                self._pre_pause_user_scrolled_away = None
                print(f"[SmartScroller] Tracking resumed, restored user_scrolled_away={self._user_scrolled_away}, pause_count={self._pause_count}")
            else:
                # Should not happen, but fallback
                self._user_scrolled_away = value < max_val - 10
                print(f"[SmartScroller] Tracking resumed, calculated user_scrolled_away={self._user_scrolled_away}, pause_count={self._pause_count}")
            
            self._auto_scroll_enabled = not self._user_scrolled_away
        else:
            # Still paused, keep current state unchanged
            print(f"[SmartScroller] Tracking partially resumed, pause_count={self._pause_count}")
    def force_scroll_to_bottom(self):
        """Force scroll to bottom regardless of auto-scroll setting."""
        self._do_scroll_to_bottom()