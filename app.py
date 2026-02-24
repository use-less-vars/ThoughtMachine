import streamlit as st
import json
from typing import Optional
from agent_core import run_agent_stream, AgentConfig
from tools import TOOL_REGISTRY  # optional, for display

st.set_page_config(page_title="Agent Cockpit", layout="wide")
st.title("🤖 Agent Cockpit")

# Initialize session state
if "stop" not in st.session_state:
    st.session_state.stop = False
if "api_key" not in st.session_state:
    st.session_state.api_key = ""
if "conversation_logs" not in st.session_state:
    st.session_state.conversation_logs = []
if "final_answer" not in st.session_state:
    st.session_state.final_answer = None
if "total_usage" not in st.session_state:
    st.session_state.total_usage = {"input": 0, "output": 0}
if "query_input" not in st.session_state:
    st.session_state.query_input = ""

# Helper functions for API key loading (same as before)
def extract_api_key_from_file(filepath: str) -> Optional[str]:
    import re
    key_pattern = re.compile(r'sk-[A-Za-z0-9]+')
    try:
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    match = key_pattern.search(line)
                    if match:
                        return match.group()
    except Exception:
        pass
    return None

def find_api_key_in_directory(directory: str = "~/.deepseek/keys") -> Optional[str]:
    from pathlib import Path
    dir_path = Path(directory).expanduser()
    if not dir_path.exists():
        return None
    for file in dir_path.iterdir():
        if file.is_file():
            key = extract_api_key_from_file(str(file))
            if key:
                return key
    return None

# ----------------------------
# Sidebar
# ----------------------------
with st.sidebar:
    st.header("Settings")

    if st.session_state.api_key:
        st.success("✅ API Key loaded")
    else:
        st.error("❌ No API Key")

    api_key_input = st.text_input(
        "API Key",
        type="password",
        value=st.session_state.api_key,
        help="Enter your API key manually, or load from file below."
    )
    st.session_state.api_key = api_key_input

    st.divider()
    st.subheader("Load API key from file")

    key_file_path = st.text_input("Single key file path", value=".api_key")
    if st.button("Load from this file"):
        key = extract_api_key_from_file(key_file_path)
        if key:
            st.session_state.api_key = key
            st.success("Key loaded from file!")
        else:
            st.error("No valid API key found in that file.")

    scan_dir = st.text_input("Directory to scan for key files", value="~/.deepseek/keys")
    if st.button("Scan directory"):
        key = find_api_key_in_directory(scan_dir)
        if key:
            st.session_state.api_key = key
            st.success(f"Key found in directory!")
        else:
            st.error("No valid API key found in that directory.")

    st.divider()
    st.subheader("Agent Settings")
    max_turns = st.slider("Max Turns", 1, 20, 10)
    temperature = st.slider("Temperature", 0.0, 1.0, 0.2, 0.1)
    extra_system = st.text_area("Extra system prompt (optional)", height=100)

    st.divider()
    if st.button("🛑 Stop Agent"):
        st.session_state.stop = True
        st.warning("Stop signal sent – agent will stop after current turn.")

    st.divider()
    st.subheader("Example Queries")
    examples = [
        "Write a short poem about AI and save it to poem.txt",
        "What is 15 * 27?",
        "List all files in the current directory",
        "Read the file poem.txt",
        "Calculate (24 + 36) / 12 and then write the result to result.txt"
    ]
    selected_example = st.selectbox("Choose an example", [""] + examples)
    if selected_example and st.button("Use this example"):
        st.session_state.query_input = selected_example

    st.divider()
    if st.button("🗑️ Clear Conversation"):
        st.session_state.conversation_logs = []
        st.session_state.final_answer = None
        st.session_state.total_usage = {"input": 0, "output": 0}
        st.session_state.query_input = ""

    # Show registered tools (optional)
    with st.expander("🔧 Available Tools"):
        st.write(list(TOOL_REGISTRY.keys()))

# ----------------------------
# Main area
# ----------------------------
col1, col2 = st.columns([3, 1])
with col1:
    query = st.text_area("Your query:", value=st.session_state.query_input, height=150, key="query_input")
with col2:
    st.write("")
    st.write("")
    run_button = st.button("🚀 Run Agent", type="primary", use_container_width=True)

# Token usage display
usage_col1, usage_col2, usage_col3 = st.columns(3)
with usage_col1:
    st.metric("Total Input Tokens", st.session_state.total_usage["input"])
with usage_col2:
    st.metric("Total Output Tokens", st.session_state.total_usage["output"])
with usage_col3:
    st.metric("Total Turns", len(st.session_state.conversation_logs))

# Placeholders
final_answer_placeholder = st.empty()
logs_placeholder = st.empty()
status_placeholder = st.empty()

# Display existing final answer
if st.session_state.final_answer:
    with final_answer_placeholder.container():
        st.success("✅ Final Answer")
        st.markdown(f"> {st.session_state.final_answer}")

# Display existing conversation logs
if st.session_state.conversation_logs:
    with logs_placeholder.container():
        st.subheader("📜 Conversation Turns")
        for turn_data in st.session_state.conversation_logs:
            with st.expander(f"Turn {turn_data['turn']} – Tool: {turn_data['tool_call']['tool']}", expanded=False):
                st.markdown("**Assistant called:**")
                st.json(turn_data['tool_call'])
                st.markdown(f"**Result:** {turn_data['tool_result']}")
                if turn_data.get('usage'):
                    st.caption(f"Tokens: in={turn_data['usage'].get('input',0)} out={turn_data['usage'].get('output',0)}")

# ----------------------------
# Run agent logic
# ----------------------------
if run_button and query and st.session_state.api_key:
    # Reset state for new run
    st.session_state.stop = False
    st.session_state.conversation_logs = []
    st.session_state.final_answer = None
    st.session_state.total_usage = {"input": 0, "output": 0}

    # Clear placeholders
    final_answer_placeholder.empty()
    logs_placeholder.empty()
    status_placeholder.empty()

    # Create config
    config = AgentConfig(
        api_key=st.session_state.api_key,
        max_turns=max_turns,
        temperature=temperature,
        extra_system=extra_system if extra_system.strip() else None,
        stop_check=lambda: st.session_state.stop
    )

    status_placeholder.info("🚀 Starting agent...")

    try:
        for event in run_agent_stream(query, config):
            if st.session_state.stop:
                # Stop signal already handled inside run_agent_stream, but we break just in case
                break

            if event["type"] == "turn":
                turn = event["turn"]
                tool_call = event["tool_call"]
                tool_result = event["tool_result"]
                usage = event.get("usage", {})

                # Update totals
                st.session_state.total_usage["input"] = usage.get("total_input", 0)
                st.session_state.total_usage["output"] = usage.get("total_output", 0)

                # Store turn
                st.session_state.conversation_logs.append({
                    "turn": turn,
                    "tool_call": tool_call,
                    "tool_result": tool_result,
                    "usage": usage
                })

                # Update logs display
                with logs_placeholder.container():
                    st.subheader("📜 Conversation Turns")
                    for turn_data in st.session_state.conversation_logs:
                        with st.expander(f"Turn {turn_data['turn']} – Tool: {turn_data['tool_call']['tool']}", expanded=False):
                            st.markdown("**Assistant called:**")
                            st.json(turn_data['tool_call'])
                            st.markdown(f"**Result:** {turn_data['tool_result']}")
                            if turn_data.get('usage'):
                                st.caption(f"Tokens: in={turn_data['usage'].get('input',0)} out={turn_data['usage'].get('output',0)}")

                status_placeholder.success(f"Turn {turn} completed.")

            elif event["type"] == "final":
                st.session_state.final_answer = event["content"]
                with final_answer_placeholder.container():
                    st.success("✅ Final Answer")
                    st.markdown(f"> {st.session_state.final_answer}")

                # Update final usage
                usage = event.get("usage", {})
                st.session_state.total_usage["input"] = usage.get("total_input", 0)
                st.session_state.total_usage["output"] = usage.get("total_output", 0)

                status_placeholder.success("✅ Agent finished successfully.")
                break

            elif event["type"] == "stopped":
                status_placeholder.warning("⏹️ Agent stopped by user.")
                break

            elif event["type"] == "max_turns":
                status_placeholder.warning(f"⚠️ Max turns ({max_turns}) reached.")
                break

            elif event["type"] == "error":
                status_placeholder.error(f"Error: {event.get('message', 'Unknown')}")
                break

            # Update token metrics in sidebar (they'll update on next st run)
            # We can't dynamically update the metric widgets, but they will refresh on next rerun.
            # To force an immediate update, we could use st.rerun(), but that would interrupt the loop.
            # So we just let them update after the run completes.

        # After loop, we might need to refresh the token display.
        # Since the loop is done, we can just let the next rerun show the final values.

    except Exception as e:
        status_placeholder.error(f"An unexpected error occurred: {e}")

elif run_button and not st.session_state.api_key:
    st.error("Please enter or load an API key.")
elif run_button and not query:
    st.error("Please enter a query.")