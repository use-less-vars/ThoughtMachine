import threading
import os
import queue
import traceback
from agent.config import AgentConfig
from agent import Agent
from typing import Optional, List, Dict, Any
from PyQt6.QtCore import QObject, pyqtSignal
from agent.logging import log

class AgentController(QObject):
    """
    Runs the agent in a background thread and provides thread‑safe control
    via start/stop/pause/resume and a queue for receiving events.
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
        self._initial_conversation = None
        self._agent_override = None
        self.agent = None
        self.current_session_id = None
        self.query_queue = queue.Queue()
        self._keep_alive = True
        self._pause_requested = False
        self._processing_query = False

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
        self._initial_conversation = None
        self.agent = None
        self._keep_alive = True
        self._pause_requested = False
        self._processing_query = False
        self.current_session_id = None
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            log('DEBUG', 'core.controller', f'Reset to initial state')

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
        log('DEBUG', 'core.controller', f'start called with query: {query[:50]}...')
        self._cleanup_if_thread_dead()
        if self._running:
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
        self._query = query
        self._config = resolved_config
        self._session = session
        self.current_session_id = session.session_id if session is not None else None
        self.query_queue.put(query)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self._running = True
        self.thread.start()

    def stop(self):
        """Request the agent to pause after the current turn/tool."""
        self.pause()

    def continue_session(self, query: str):
        """Submit a new query to the already running agent."""
        log('DEBUG', 'core.controller', f"continue_session called: query='{query[:50]}...' is_running={self.is_running} pause_event.is_set={self.pause_event.is_set()}")
        if os.environ.get('PAUSE_DEBUG'):
            log('WARNING', 'presenter.pause_flow', f"Controller.continue_session: query='{query[:50]}...', is_running={self.is_running}, pause_event.is_set={self.pause_event.is_set()}, _pause_requested={self._pause_requested}")
        if not self.is_running:
            debug_msg = f'[Controller] Agent not running, cannot continue. _running={self._running}, thread alive={(self.thread.is_alive() if self.thread else False)}'
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                log('DEBUG', 'core.controller', debug_msg)
            if os.environ.get('PAUSE_DEBUG'):
                log('WARNING', 'presenter.pause_flow', f'Controller.continue_session: {debug_msg}')
            raise RuntimeError(f'Agent controller not running: {debug_msg}')
        if os.environ.get('PAUSE_DEBUG'):
            log('WARNING', 'presenter.pause_flow', f'Controller.continue_session: calling resume() and queuing query')
        self.resume()
        self.query_queue.put(query)
        log('DEBUG', 'core.controller', f'Query queued, queue size approx {self.query_queue.qsize()}')
        if os.environ.get('PAUSE_DEBUG'):
            log('WARNING', 'presenter.pause_flow', f'Controller.continue_session: query queued, queue size={self.query_queue.qsize()}')

    def request_pause(self):
        """Request agent to pause after current turn."""
        log('DEBUG', 'core.controller', f'request_pause called: is_running={self.is_running} _processing_query={self._processing_query} pause_event.is_set={self.pause_event.is_set()}')
        if not self.is_running:
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                log('DEBUG', 'core.controller', f'Agent not running, nothing to pause')
            return
        if self._processing_query:
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                log('DEBUG', 'core.controller', f'Agent processing query, calling pause()')
            self.pause()
        else:
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                log('DEBUG', 'core.controller', f'Agent idle, sending paused event directly')
            self.pause_event.clear()
            self._pause_requested = True
            if hasattr(self, 'agent') and self.agent is not None and hasattr(self.agent, 'request_pause'):
                self.agent.request_pause()
            self._emit_event({'type': 'paused'})
            if hasattr(self, 'agent') and self.agent is not None:
                from session.context_builder import ContextBuilder
                if hasattr(self.agent, 'conversation'):
                    original_len = len(self.agent.conversation)
                    self.agent.conversation = ContextBuilder._cleanup_orphaned_tool_messages(self.agent.conversation)
                    if original_len != len(self.agent.conversation) and os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                        log('WARNING', 'core.controller', f'Cleaned {original_len - len(self.agent.conversation)} orphaned tool messages on idle pause')

    def get_conversation(self) -> Optional[List[Dict[str, Any]]]:
        """Return the current conversation from the agent, if available."""
        if self.agent:
            return self.agent.conversation.copy() if self.agent.conversation is not None else None
        return None

    def update_runtime_params(self, **kwargs):
        """Forward runtime parameter updates to the agent if available."""
        if self.agent is not None:
            self.agent.update_runtime_params(**kwargs)

    def restart_session(self):
        """Restart agent with cleared history."""
        if not self.is_running:
            return
        if self.agent:
            self.agent.request_reset()
        self.query_queue.put('[RESET]')

    def pause(self):
        """Pause the agent before the next turn (finishes current turn first)."""
        log('DEBUG', 'core.controller', f'pause() called, clearing pause_event, setting _pause_requested=True')
        self.pause_event.clear()
        self._pause_requested = True
        if hasattr(self, 'agent') and self.agent is not None and hasattr(self.agent, 'request_pause'):
            self.agent.request_pause()
        if hasattr(self, 'agent') and self.agent is not None:
            from session.context_builder import ContextBuilder
            if hasattr(self.agent, 'conversation'):
                original_len = len(self.agent.conversation)
                self.agent.conversation = ContextBuilder._cleanup_orphaned_tool_messages(self.agent.conversation)
                if original_len != len(self.agent.conversation) and os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
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
        """Emit event both to queue and signal."""
        event['session_id'] = self.current_session_id
        self.event_queue.put(event)
        log('DEBUG', 'core.controller', f"Emitting event_occurred: {event.get('type')}")
        self.event_occurred.emit(event)
        content_event_types = {'user_query', 'turn', 'tool_call', 'tool_result', 'final', 'llm_request', 'llm_response', 'raw_response'}
        if event.get('type') in content_event_types:
            log('DEBUG', 'core.controller', f"Emitting conversation_updated for event type {event.get('type')}")
            self.conversation_updated.emit(self.current_session_id if self.current_session_id else '')

    def _run(self):
        """Internal method that runs in the background thread."""
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            log('DEBUG', 'core.controller', f'_run started')
        try:

            def should_stop():
                log('DEBUG', 'core.controller', f'should_stop called, pause_event.is_set={self.pause_event.is_set()}, stop_event.is_set={self.stop_event.is_set()}, _pause_requested={self._pause_requested}')
                if self.stop_event.is_set():
                    if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                        log('DEBUG', 'core.controller', f'should_stop: stop_event is set, returning True')
                    return True
                if not self.pause_event.is_set():
                    if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                        log('DEBUG', 'core.controller', f'should_stop: pause_event not set, returning PAUSED')
                    return 'PAUSED'
                if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                    log('DEBUG', 'core.controller', f'should_stop: not paused, returning False')
                return False
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
                agent = Agent(run_config, session=self._session if hasattr(self, '_session') else None)
                self.agent = agent
            while self._keep_alive:
                stop_result = should_stop()
                if stop_result:
                    if stop_result == 'PAUSED':
                        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                            log('DEBUG', 'core.controller', f'PAUSED returned, waiting on pause_event')
                        self.pause_event.wait()
                        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                            log('DEBUG', 'core.controller', f'Resumed from pause_event.wait()')
                        continue
                    continue
                log('DEBUG', 'core.controller', f'Before query_queue.get, queue size: {self.query_queue.qsize()}')
                try:
                    query = self.query_queue.get(timeout=1.0)
                    log('DEBUG', 'core.controller', f"Got query from queue: '{query[:50]}...'")
                except queue.Empty:
                    log('DEBUG', 'core.controller', f'Queue empty after timeout')
                    continue
                if query == '[RESET]':
                    agent.reset()
                    continue
                if query == '[PAUSE]':
                    if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                        log('DEBUG', 'core.controller', f'Pause requested')
                    self._emit_event({'type': 'paused'})
                    continue
                log('DEBUG', 'core.controller', f'Processing query: {query[:50]}...')
                self._processing_query = True
                for event in agent.process_query(query):
                    log('DEBUG', 'core.controller', f"Event: {event['type']}")
                    self._emit_event(event)
                    if event['type'] == 'paused':
                        self._pause_requested = False
                        break
                    if event['type'] in ('stopped', 'error', 'max_turns'):
                        log('DEBUG', 'core.controller', f"Terminal event {event['type']} detected, treating as pause")
                        self._pause_requested = False
                        self._emit_event({'type': 'paused'})
                        break
                    elif event['type'] in ('final', 'user_interaction_requested'):
                        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                            log('DEBUG', 'core.controller', f'Sending paused event')
                        self._emit_event({'type': 'paused'})
                        break
                    if not self.pause_event.is_set():
                        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                            log('DEBUG', 'core.controller', f'pause_event not set between events, breaking loop')
                        self._pause_requested = False
                        self._emit_event({'type': 'paused'})
                        break
                self._processing_query = False
                if not self._keep_alive:
                    log('DEBUG', 'core.controller', f'_keep_alive=False, breaking outer loop')
                    break
        except Exception as e:
            log('ERROR', 'core.controller', f'Exception in _run: {e}')
            traceback.print_exc()
            self._running = False
            self._emit_event({'type': 'error', 'error_type': 'CONTROLLER_ERROR', 'message': str(e), 'traceback': traceback.format_exc()})
        finally:
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                log('DEBUG', 'core.controller', f'Finally block: thread finishing')
            self._emit_event({'type': 'thread_finished'})
            self._running = False