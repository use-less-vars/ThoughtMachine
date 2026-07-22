"""
Trace the full config flow: _translate_frontend_config -> bridge.start / bridge.apply_config -> AgentConfig.

This simulates creating an Engineer session as the frontend would.
"""
import io
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Capture log output
log_capture = io.StringIO()
log_handler = logging.StreamHandler(log_capture)
log_handler.setLevel(logging.INFO)

agent_logger = logging.getLogger('agent')
agent_logger.setLevel(logging.INFO)
agent_logger.addHandler(log_handler)
agent_logger.propagate = True

from web_ui.backend.server import _translate_frontend_config
from web_ui.backend.bridge import WebAgentBridge
from session.store import FileSystemSessionStore


def simulate_engineer_session():
    """Simulate what the frontend does when creating an Engineer session."""
    results = {}

    # Step 1: Frontend sends start_session with mode='engineer'
    frontend_config = {
        "mode": "engineer",
        "provider": "openai",
        "model": "gpt-4",
        "temperature": 0.7,
        "max_turns": 50,
    }
    results['frontend_input'] = dict(frontend_config)

    # Step 2: _translate_frontend_config
    translated = _translate_frontend_config(frontend_config)
    results['after_translate'] = {
        'mode': translated.get('mode'),
        'enabled_tools': translated.get('enabled_tools'),
    }

    # Step 3: Create bridge and apply_config
    with tempfile.TemporaryDirectory() as tmpdir:
        session_store = FileSystemSessionStore(
            sessions_dir=str(Path(tmpdir) / 'sessions'),
            state_dir=str(Path(tmpdir) / 'state'),
        )
        bridge = WebAgentBridge(session_store=session_store)

        # apply_config first (like the frontend does)
        apply_result = bridge.apply_config(translated)
        results['apply_config_result'] = apply_result

        if apply_result.get('success'):
            config_after_apply = bridge.get_config()
            results['after_apply_config'] = {
                'mode': config_after_apply.mode,
                'enabled_tools': list(config_after_apply.enabled_tools)[:10],
                'tool_count': len(config_after_apply.enabled_tools),
            }

        # Step 4: bridge.start (like start_session)
        log_capture.truncate(0)
        log_capture.seek(0)

        try:
            bridge.start(query="Write a Python script to parse CSV files",
                         config_dict=translated)
            config_after_start = bridge.get_config()
            results['after_start'] = {
                'mode': config_after_start.mode,
                'enabled_tools': list(config_after_start.enabled_tools)[:10],
                'tool_count': len(config_after_start.enabled_tools),
            }
        except Exception as exc:
            results['start_error'] = str(exc)

    return results


if __name__ == '__main__':
    print("=" * 70)
    print("Simulating Engineer session creation...")
    print("=" * 70)

    results = simulate_engineer_session()

    print("\n--- Results ---")
    for key, val in results.items():
        print(f"\n{key}:")
        if isinstance(val, dict):
            for k, v in val.items():
                print(f"  {k}: {v}")
        else:
            print(f"  {val}")

    # Print captured logs
    print("\n--- Captured TRACE Logs ---")
    log_output = log_capture.getvalue()
    trace_lines = [l for l in log_output.split('\n') if '[TRACE]' in l]
    if trace_lines:
        for line in trace_lines:
            print(line)
    else:
        print("(No TRACE logs captured)")
        print("\n--- All logs (first 2000 chars) ---")
        print(log_output[:2000])
