import threading
import os
import sys
import queue
import traceback
import uuid
from agent.config import AgentConfig
from agent import Agent
from typing import Optional, Callable, List, Dict, Any
try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _HAS_QT = True
except ImportError:
    _HAS_QT = False
    # Dummy QObject for non-Qt environments
    class QObject:
        """Stand-in when PyQt6 is not installed."""
        pass

    class _DummySignal:
        """Stand-in for pyqtSignal when PyQt6 is not installed."""
        def __init__(self, *args, **kwargs):
            pass
        def emit(self, *args, **kwargs):
            pass
        def connect(self, *args, **kwargs):
            pass
        def disconnect(self, *args, **kwargs):
            pass

    def pyqtSignal(*args, **kwargs):
        """Return a dummy signal object when PyQt6 is not available."""
        return _DummySignal()
from agent.logging import log
from agent.core.state import ExecutionState

class AgentController(QObject):
    """
    Runs the agent in a background thread and provides thread‑safe control
    via start/stop/pause/resume and a queue for receiving events.

    Properties
    ----------
    is_busy : bool
        True when the agent is RUNNING or PAUSING.
        Safe to call from synchronous code (WebSocket handlers, etc.).
        Returns False when state is READY or no agent exists.
        Note: This reads the agent's ExecutionState which is set asynchronously
        inside the background thread. After start() returns, the agent thread
        may not have reached RUNNING yet; consumers that need to poll should
        allow a brief settling period or rely on the controller's own _running
        flag for the synchronous "thread is alive" check.
    """
    event_occurred = pyqtSignal(dict)
    conversation_updated = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.event_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.thread = None
        self._running = False
        self._agent_override = None
        self.agent = None
        self.current_session_id = None
        self.query_queue = queue.Queue()
        self._keep_alive = True
        self._pause_requested = False
        self._processing_query = False
        # Plain Python callbacks (non-Qt consumers like Web UI)
        self._config: Optional[AgentConfig] = None
        self._event_callbacks: List[Callable[[Dict[str, Any]], None]] = []

    def set_event_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """
        Register a plain Python callback for events.
        Unlike the pyqtSignal, this works without a Qt event loop.
        """
        if callback not in self._event_callbacks:
            self._event_callbacks.append(callback)

    def _cleanup_if_thread_dead(self):
        """Check if background thread is dead and reset state if needed."""
        log('DEBUG', 'core.controller', f"_cleanup_if_thread_dead called, thread={('alive' if self.thread and self.thread.is_alive() else 'dead/None')}")
        if self.thread is not None and (not self.thread.is_alive()):
            log('DEBUG', 'core.controller', f'Thread dead, cleaning up state')
            self._running = False
            self.thread = None
            self.agent = None
            self._keep_alive = True
            self._pause_requested = False
            self._processing_query = False
            log('DEBUG', 'core.controller', f'Cleaned up dead thread, _running={self._running}')

    def reset(self):
        """Reset controller to initial state, clearing all queues and events."""
        log('DEBUG', 'core.controller', 'reset() called')
        while True:
            try:
                self.event_queue.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self.query_queue.get_nowait()
            except queue.Empty:
                break
        self.stop_event.clear()
        self.pause_event.set()
        self.thread = None
        self._running = False
        self.agent = None
        self._keep_alive = True
        self._pause_requested = False
        self._processing_query = False
        self.current_session_id = None
        log('DEBUG', 'core.controller', f'Reset to initial state')

    @property
    def is_busy(self) -> bool:
        """True when the agent is RUNNING or PAUSING.

        Safe to call from synchronous code (WebSocket handlers, etc.).
        Returns False when state is READY or no agent exists.
        """
        if self.agent is None:
            return False
        return self.agent.state.execution_state in (ExecutionState.RUNNING, ExecutionState.PAUSING)

    @property
    def is_running(self):
        """Return True if the agent thread is alive and not shutting down."""
        if not self._running:
            log('DEBUG', 'core.controller', f'is_running: _running=False, returning False (thread alive={(self.thread.is_alive() if self.thread else False)})')
            return False
        if self.thread is not None and self.thread.is_alive():
            log('DEBUG', 'core.controller', f'is_running: thread alive, returning True (_running={self._running}, pause_event.is_set={self.pause_event.is_set()}, _pause_requested={self._pause_requested})')
            return True
        log('DEBUG', 'core.controller', f'is_running: thread dead or None, cleaning up (thread={self.thread})')
        self._cleanup_if_thread_dead()
        log('DEBUG', 'core.controller', f'is_running: after cleanup, _running={self._running}')
        return self._running

    def get_config(self):
        """Return the current AgentConfig being used."""
        return self._config

    # ── New unified API ────────────────────────────────────────────────────

    def set_session(self, session, config: AgentConfig) -> None:
        """
        Store a session and config for later use by process_query().

        Call this before the first process_query() call to configure
        the session the agent should use.

        Args:
            session: A Session instance (with user_history, session_id, etc.).
            config: An AgentConfig instance (api_key, model, etc.).
        """
        self._session = session
        self._config = config
        self._agent_override = None
        self.current_session_id = session.session_id if session is not None else None
        log('DEBUG', 'core.controller',
            f'set_session: session_id={self.current_session_id}, '
            f'config provider={config.provider_type}, model={config.model}')

    def update_config(self, config: AgentConfig) -> None:
        """
        Set a pending configuration update.

        If the agent already exists the update is forwarded via the
        mailbox pattern; otherwise it is stored and picked up when
        process_query() starts a new thread.

        Args:
            config: New AgentConfig to apply.
        """
        self._config = config
        if self.agent is not None:
            self.agent.request_config_update(config)
            log('DEBUG', 'core.controller',
                f'update_config: forwarded to agent, provider={config.provider_type}')
        else:
            log('DEBUG', 'core.controller',
                f'update_config: stored for next start, provider={config.provider_type}')

    def process_query(self, query: str) -> None:
        """
        Unified entry point for submitting a query to the agent.

        Automatically handles three scenarios:

        1. **No agent exists** – starts a new background thread with
           the session and config previously stored via set_session().

        2. **Thread is alive** (possibly paused) – resumes the agent
           and queues the query so it is processed on the next turn.

        3. **Thread is dead** – cleans up stale state and starts
           a fresh thread.

        Call ``set_session(session, config)`` once before the first
        ``process_query()`` call.

        Args:
            query: The user query string.
        """
        self._cleanup_if_thread_dead()

        if self.agent is None:
            # ── Scenario 1: No agent — start fresh thread ──
            self.stop_event.clear()
            self.pause_event.set()
            self._keep_alive = True
            self._pause_requested = False
            self._processing_query = False
            self._query = query
            self.current_session_id = (
                self._session.session_id
                if getattr(self, '_session', None) is not None
                else None
            )
            self.query_queue.put(query)
            self.thread = threading.Thread(target=self._run, daemon=True)
            self._running = True
            log('DEBUG', 'core.controller',
                f'process_query: starting new thread for query={query[:80]!r}...')
            self.thread.start()

        elif self.thread is not None and self.thread.is_alive():
            # ── Scenario 2: Thread alive — continue session ──
            if self.agent is None:
                raise RuntimeError(
                    'Agent is None (creation failed). Cannot process query.'
                )
            log('DEBUG', 'core.controller',
                f'process_query: continuing alive thread, query={query[:80]!r}...')
            self.resume()
            self.query_queue.put(query)

        else:
            # ── Scenario 3: Thread dead — restart fresh ──
            log('DEBUG', 'core.controller',
                'process_query: thread dead, cleaning up and restarting')
            self._running = False
            self.thread = None
            self.agent = None
            self.stop_event.clear()
            self.pause_event.set()
            self._keep_alive = True
            self._pause_requested = False
            self._processing_query = False
            self._query = query
            self.current_session_id = (
                self._session.session_id
                if getattr(self, '_session', None) is not None
                else None
            )
            self.query_queue.put(query)
            self.thread = threading.Thread(target=self._run, daemon=True)
            self._running = True
            self.thread.start()

    # ── Deprecated wrappers ───────────────────────────────────────────────

    def start(self, query: str, config: AgentConfig=None, session=None, preset_name: str=None, **overrides):
        """
        Start the agent with the given query and configuration.

        Args:
            query: The user query string.
            config: An AgentConfig instance (api_key, model, etc.). Mutually exclusive with preset_name.
            session: Optional Session instance to associate with this run (for history persistence).
            preset_name: Name of a preset to use instead of config. If provided, config is ignored.
            **overrides: Additional config overrides when using preset_name.
        """
        log('WARNING', 'core.controller', 'start() is deprecated. Use set_session() + process_query() instead.')
        log('INFO', 'core.controller', 'controller.start() ENTERED')
        log('INFO', 'core.controller', f'start called with query={query[:80]!r}..., config type={type(config).__name__}, session={session.session_id if session else None}, preset_name={preset_name!r}')
        self._cleanup_if_thread_dead()
        if self._running:
            if self.thread is not None and self.thread.is_alive() and self._keep_alive:
                # Thread exists and is in keep-alive mode; route to continue_session.
                self.continue_session(query)
                return
            raise RuntimeError('Agent is already running. Stop it first.')
        self.stop_event.clear()
        self.pause_event.set()
        self._keep_alive = True
        self._pause_requested = False
        self._processing_query = False
        if preset_name is not None:
            if config is not None:
                raise ValueError('Cannot specify both config and preset_name')
            from agent import Agent
            agent = Agent.from_preset(preset_name, session=session, **overrides)
            resolved_config = agent.config
            self._agent_override = agent
        else:
            if config is None:
                raise ValueError('Must provide either config or preset_name')
            resolved_config = config
            self._agent_override = None
        if preset_name is not None:
            log('DEBUG', 'core.controller', f"[CONTROLLER start] preset agent id={id(agent)}, session={session.session_id if session else 'None'}")
        else:
            log('DEBUG', 'core.controller', f"[CONTROLLER start] config mode, _agent_override reset, session={session.session_id if session else 'None'}")
        self._query = query
        self._config = resolved_config
        self._session = session
        self.current_session_id = session.session_id if session is not None else None
        self.query_queue.put(query)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self._running = True
        log('DEBUG', 'core.controller', 'start(): _running set to True, starting thread')
        self.thread.start()
        log('DEBUG', 'core.controller', 'start(): thread.start() returned')

    def stop(self):
        """Request the agent to pause after the current turn/tool."""
        log('DEBUG', 'core.controller', 'stop() called - delegating to pause()')
        self.pause()

    def continue_session(self, query: str):
        """Submit a new query to the already running agent."""
        log('WARNING', 'core.controller', 'continue_session() is deprecated. Use process_query() instead.')
        log('DEBUG', 'core.controller', f"[CONTROLLER continue_session] self.agent={'exists' if hasattr(self, 'agent') and self.agent else 'MISSING'}, agent.id={id(self.agent) if hasattr(self, 'agent') and self.agent else 'N/A'}")
        log('DEBUG', 'core.controller', f"continue_session called: query='{query[:50]}...' is_running={self.is_running} pause_event.is_set={self.pause_event.is_set()}")
        if os.environ.get('PAUSE_DEBUG'):
            log('WARNING', 'presenter.pause_flow', f"Controller.continue_session: query='{query[:50]}...', is_running={self.is_running}, pause_event.is_set={self.pause_event.is_set()}, _pause_requested={self._pause_requested}")
        if not self.is_running:
            debug_msg = f'[Controller] Agent not running, cannot continue. _running={self._running}, thread alive={(self.thread.is_alive() if self.thread else False)}'
            log('DEBUG', 'core.controller', debug_msg)
            if os.environ.get('PAUSE_DEBUG'):
                log('WARNING', 'presenter.pause_flow', f'Controller.continue_session: {debug_msg}')
            raise RuntimeError(f'Agent controller not running: {debug_msg}')
        if self.agent is None:
            debug_msg = '[Controller] Agent is None (creation failed). Cannot continue session.'
            log('ERROR', 'core.controller', debug_msg)
            raise RuntimeError(debug_msg)
        if os.environ.get('PAUSE_DEBUG'):
            log('WARNING', 'presenter.pause_flow', f'Controller.continue_session: calling resume() and queuing query')
        self.resume()
        self.query_queue.put(query)
        log('DEBUG', 'core.controller', f'Query queued, queue size approx {self.query_queue.qsize()}')
        if os.environ.get('PAUSE_DEBUG'):
            log('WARNING', 'presenter.pause_flow', f'Controller.continue_session: query queued, queue size={self.query_queue.qsize()}')

    def request_pause(self):
        """Request agent to pause after current turn.

        Sets PAUSING state *immediately* — not deferred to a checkpoint — so the
        GUI shows feedback the moment the user presses the Pause button.
        """
        log('DEBUG', 'core.controller', f'request_pause called: is_running={self.is_running} _processing_query={self._processing_query} pause_event.is_set={self.pause_event.is_set()}')
        if not self.is_running:
            log('DEBUG', 'core.controller', f'Agent not running, nothing to pause')
            return
        if self._processing_query:
            log('DEBUG', 'core.controller', f'Agent processing query, calling pause()')
            self.pause()
            # ── PAUSING state set immediately (processing branch) ──
            if self.agent is not None and self.agent.state.execution_state == ExecutionState.RUNNING:
                self.agent.state.execution_state = ExecutionState.PAUSING
                self._emit_event({
                    'type': 'execution_state_change',
                    'old_state': 'running',
                    'new_state': 'pausing',
                })
                log('DEBUG', 'core.controller', 'PAUSING state set immediately on pause request (processing)')
        else:
            log('DEBUG', 'core.controller', f'Agent idle (not processing query); setting PAUSING state then emitting session_stop')
            self.pause_event.clear()
            self._pause_requested = True
            # ── PAUSING state set immediately (idle branch) ──
            if self.agent is not None:
                old_state = self.agent.state.execution_state.value
                self.agent.state.execution_state = ExecutionState.PAUSING
                self._emit_event({
                    'type': 'execution_state_change',
                    'old_state': old_state,
                    'new_state': 'pausing',
                })
                log('DEBUG', 'core.controller', 'PAUSING state set immediately on pause request (idle)')
            if hasattr(self, 'agent') and self.agent is not None and hasattr(self.agent, 'request_pause'):
                self.agent.request_pause()
            if hasattr(self, 'agent') and self.agent is not None:
                from session.context_builder import ContextBuilder
                if hasattr(self.agent, 'conversation'):
                    original_len = len(self.agent.conversation)
                    self.agent.conversation = ContextBuilder._cleanup_orphaned_tool_messages(self.agent.conversation)
                    if original_len != len(self.agent.conversation):
                        log('WARNING', 'core.controller', f'Cleaned {original_len - len(self.agent.conversation)} orphaned tool messages on idle pause')
            self._emit_event({'type': 'session_stop', 'stop_reason': 'paused'})

    def get_conversation(self) -> Optional[List[Dict[str, Any]]]:
        """Return the current conversation from the agent, if available."""
        if self.agent:
            return self.agent.conversation.copy() if self.agent.conversation is not None else None
        return None

    def request_config_update(self, config: AgentConfig):
        """Request a configuration update via the mailbox pattern.
        
        The agent will apply the update at the next process_query() boundary,
        deciding internally whether to hot-swap or restart.
        """
        if self.agent is not None:
            self.agent.request_config_update(config)

    def restart_agent(self, new_config: AgentConfig) -> bool:
        """
        Restart the agent with a new configuration while preserving conversation history.

        This pauses the agent, applies the new config via Agent.restart(),
        and resumes execution. Safe to call from the main/GUI thread.

        Args:
            new_config: New AgentConfig to apply

        Returns:
            True if restart was successful, False otherwise
        """
        if not self.is_running or self.agent is None:
            log('DEBUG', 'core.controller', 'Cannot restart agent: not running or no agent')
            return False

        # Request pause and wait for any in-flight processing to finish
        self.request_pause()

        import time
        timeout = 5.0
        start = time.time()
        while self._processing_query and time.time() - start < timeout:
            time.sleep(0.1)

        if self._processing_query:
            log('ERROR', 'core.controller', 'Timed out waiting for agent to pause for restart')
            return False

        try:
            # Apply new config to agent (safe since agent is paused)
            self.agent.restart(new_config)
            self._config = new_config
            log('INFO', 'core.controller', f'Agent restarted with new config: provider={new_config.provider_type}, model={new_config.model}')
            return True
        except Exception as e:
            log('ERROR', 'core.controller', f'Failed to restart agent: {e}')
            return False
        finally:
            self.resume()

    def restart_session(self):
        """Restart agent with cleared history."""
        if not self.is_running:
            return
        if self.agent:
            self.agent.request_reset()
        self.query_queue.put('[RESET]')

    def pause(self):
        """Pause the agent before the next turn (finishes current turn first)."""
        log('DEBUG', 'core.pause', f'PAUSE REQUESTED by user')
        log('DEBUG', 'core.controller', f'pause() called, clearing pause_event, setting _pause_requested=True')
        self.pause_event.clear()
        self._pause_requested = True
        # ── PAUSING state set immediately so GUI shows feedback ──
        if self.agent is not None:
            old_state = self.agent.state.execution_state.value
            self.agent.state.execution_state = ExecutionState.PAUSING
            self._emit_event({
                'type': 'execution_state_change',
                'old_state': old_state,
                'new_state': 'pausing',
            })
            log('DEBUG', 'core.pause', f'PAUSE REQUESTED by user (state before: {old_state}), execution_state_change emitted')
        if hasattr(self, 'agent') and self.agent is not None and hasattr(self.agent, 'request_pause'):
            self.agent.request_pause()
        if hasattr(self, 'agent') and self.agent is not None:
            from session.context_builder import ContextBuilder
            if hasattr(self.agent, 'conversation'):
                original_len = len(self.agent.conversation)
                self.agent.conversation = ContextBuilder._cleanup_orphaned_tool_messages(self.agent.conversation)
                if original_len != len(self.agent.conversation):
                    log('WARNING', 'core.controller', f'Cleaned {original_len - len(self.agent.conversation)} orphaned tool messages on pause')

    def resume(self):
        """Resume a paused agent."""
        log('DEBUG', 'core.controller', f'resume() called, setting pause_event, clearing _pause_requested')
        if os.environ.get('PAUSE_DEBUG'):
            log('WARNING', 'presenter.pause_flow', f'Controller.resume: setting pause_event, clearing _pause_requested')
        self.pause_event.set()
        self._pause_requested = False
        if hasattr(self, 'agent') and self.agent is not None:
            if hasattr(self.agent, '_pause_requested'):
                log('DEBUG', 'core.controller', f'Clearing agent._pause_requested (was {self.agent._pause_requested})')
                if os.environ.get('PAUSE_DEBUG'):
                    log('WARNING', 'presenter.pause_flow', f'Controller.resume: clearing agent._pause_requested')
                self.agent._pause_requested = False

    def _emit_event(self, event):
        """Emit event to queue, signal, and plain callbacks."""
        event['session_id'] = self.current_session_id
        self.event_queue.put(event)
        log('DEBUG', 'core.controller', f"Emitting event_occurred: {event.get('type')}")
        # Qt signal path (requires QApplication running)
        try:
            self.event_occurred.emit(event)
        except RuntimeError:
            pass  # No QApplication – safe to ignore
        # Plain callback path (works without Qt — used by Web UI)
        for cb in self._event_callbacks:
            try:
                cb(event)
            except Exception:
                import traceback as _tb
                _tb.print_exc()
        content_event_types = {'user_query', 'turn', 'tool_call', 'tool_result', 'final', 'llm_request', 'llm_response', 'raw_response'}
        if event.get('type') in content_event_types:
            log('DEBUG', 'core.controller', f"Emitting conversation_updated for event type {event.get('type')}")
            try:
                self.conversation_updated.emit(self.current_session_id if self.current_session_id else '')
            except RuntimeError:
                pass

    def _run(self):
        """Internal method that runs in the background thread."""
        log('INFO', 'core.controller', f'_run thread STARTED (threading.get_ident={threading.get_ident()})')
        log('INFO', 'core.controller', f'_run: self.agent={id(self.agent) if hasattr(self, "agent") and self.agent is not None else None}')
        if hasattr(self, 'agent') and self.agent is not None:
            log('DEBUG', 'core.controller', f"[CONTROLLER _run] entering with self.agent={id(self.agent)}, agent.conversation len={len(self.agent.conversation)}, first roles={[m.get('role') for m in self.agent.conversation[:3]]}")

        def should_stop():
            log('DEBUG', 'core.pause', f'stop_check called, pause_event.is_set()={self.pause_event.is_set()}')
            if self.stop_event.is_set():
                log('DEBUG', 'core.controller', f'should_stop: stop_event is set, returning True')
                return True
            if not self.pause_event.is_set():
                log('DEBUG', 'core.controller', f'should_stop: pause_event not set, returning PAUSED')
                return 'PAUSED'
            # Not-logged: idle return (no decision made)
            return False

        log('INFO', 'core.controller', '_run(): about to create agent')
        # --- Agent creation (separate try/except to keep thread alive on failure) ---
        try:
            if hasattr(self, '_agent_override') and self._agent_override is not None:
                agent = self._agent_override
                agent.config.stop_check = should_stop
                if hasattr(self, '_session') and self._session is not None:
                    agent.session = self._session
                    agent.conversation = self._session.user_history
                    if len(self._session.user_history) > 0:
                        from session.models import SessionState
                        events = agent.state.set_session_state(SessionState.CONTINUING)
                        for event in events:
                            agent._handle_state_event(event)
                self.agent = agent
            else:
                run_config = self._config.model_copy() if hasattr(self._config, 'model_copy') else self._config
                run_config.stop_check = should_stop
                log('DEBUG', 'core.controller', "[CONTROLLER] creating new Agent instance")
                # Ensure API key is set from env vars if config doesn't have one
                api_key_val = getattr(run_config, 'api_key', None) or ''
                if not api_key_val:
                    import os
                    api_key_val = os.getenv('OPENAI_API_KEY') or os.getenv('DEEPSEEK_API_KEY') or os.getenv('OPENAI_COMPATIBLE_API_KEY') or ''
                    if api_key_val:
                        run_config.api_key = api_key_val
                        log('INFO', 'core.controller', f"[CONTROLLER] Filled missing api_key from env var")
                agent = Agent(run_config, session=self._session if hasattr(self, '_session') else None)
                self.agent = agent
            # Propagate agent's session_id so events carry it (needed for Web UI bridge)
            if self.agent and not self.current_session_id:
                self.current_session_id = self.agent.session_id or str(uuid.uuid4())
            log('INFO', 'core.controller', f'_run: Agent created successfully, agent.id={id(self.agent) if self.agent else None}')
        except Exception as e:
            log('ERROR', 'core.controller', f'_run: Agent creation FAILED: {e}')
            traceback.print_exc()
            self.agent = None
            self._emit_event({'type': 'error', 'error_type': 'AGENT_CREATION_ERROR', 'message': str(e), 'traceback': traceback.format_exc()})

        # --- Main processing loop ---
        try:
            while self._keep_alive:
                if self.agent is None:
                    # Agent creation failed; wait for stop or reset
                    log('DEBUG', 'core.controller', '_run: No agent available, waiting in agentless idle loop')
                    try:
                        query = self.query_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if query in ('[RESET]', '[STOP]'):
                        log('DEBUG', 'core.controller', '_run: Received stop/reset in agentless state, breaking')
                        break
                    # Ignore other queries when agent is None
                    continue

                log('DEBUG', 'core.pause', 'CHECKING should_stop')
                stop_result = should_stop()
                if stop_result:
                    if stop_result == 'PAUSED':
                        log('DEBUG', 'core.controller', f'PAUSED returned, waiting on pause_event')
                        self.pause_event.wait()
                        log('DEBUG', 'core.controller', f'Resumed from pause_event.wait()')
                        continue
                    continue
                # Before-query_queue log removed (idle polling noise)
                try:
                    query = self.query_queue.get(timeout=1.0)
                    log('DEBUG', 'core.controller', f"Got query from queue: '{query[:50]}...'")
                except queue.Empty:
                    # Queue-empty log removed (idle polling noise)
                    continue
                if query == '[RESET]':
                    agent.reset()
                    continue
                log('INFO', 'core.controller', f'Processing query: {query[:80]!r}...')
                self._processing_query = True
                stop_reason = None
                try:
                    # ── Controller diagnostic ───────────────────────────────────────
                    if hasattr(agent, 'conversation'):
                        log('INFO', 'core.controller', f"[CONTROLLER _run] passing query to agent. agent.conversation has {len(agent.conversation)} msgs. roles={[m.get('role') for m in agent.conversation]}")
                        log('INFO', 'core.controller', f'[CONTROLLER _run] about to call agent.process_query()')
                    else:
                        log('DEBUG', 'core.controller', "[CONTROLLER _run] agent has NO conversation attribute")
                    for event in agent.process_query(query):
                        log('DEBUG', 'core.pause', f"POST-YIELD: event_type={event['type']}")
                        self._emit_event(event)
                        if event.get('stop_reason'):
                            log('DEBUG', 'core.controller', f"Stop reason: {event['stop_reason']}, breaking loop")
                            self._pause_requested = False
                            stop_reason = event['stop_reason']
                            break
                        if not self.pause_event.is_set():
                            log('DEBUG', 'core.controller', f'pause_event not set between events, breaking loop')
                            self._pause_requested = False
                            break
                except Exception as e:
                    log('ERROR', 'core.controller', f'Unhandled exception during process_query: {e}')
                    traceback.print_exc()
                    stop_reason = 'error'
                finally:
                    self._processing_query = False
                    # Reset agent's internal execution state to READY so the next query
                    # triggers a proper ready→running transition.
                    if hasattr(self, 'agent') and self.agent is not None:
                        self.agent.state.set_execution_state(ExecutionState.READY)
                        log('DEBUG', 'core.controller', 'Resetting agent execution state to READY after query completion')
                    self._emit_event({'type': 'session_stop', 'stop_reason': stop_reason or 'completed'})
                if not self._keep_alive:
                    log('DEBUG', 'core.controller', f'_keep_alive=False, breaking outer loop')
                    break
        except Exception as e:
            log('ERROR', 'core.controller', f'Exception in _run: {e}')
            traceback.print_exc()
            self._running = False
            self._emit_event({'type': 'error', 'error_type': 'CONTROLLER_ERROR', 'message': str(e), 'traceback': traceback.format_exc()})
        finally:
            log('DEBUG', 'core.controller', f'Finally block: thread finishing')
            self._emit_event({'type': 'thread_finished'})
            self._running = False