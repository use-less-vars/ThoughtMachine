"""Slice 3: the session addendum survives a real ``SummarizeTool`` compaction.

The brief (§6 slice 3, §7 obligation 6) asks for an assertion, not an argument:
run a **real** ``SummarizeTool`` compaction and assert the addendum is still
present in the next request; then do it **twice**.  Stubbing the compaction
would encode the very assumption under test (pruner behaviour), so nothing on
this path is stubbed:

  * the session is a real ``session.models.Session`` whose addendum went through
    the real ``normalize_addendum`` (slice 1);
  * the agent is a real ``Agent``, constructed the same way
    ``tests/test_agent_stop_midstream.py`` does, so the addendum is composed into
    ``user_history[0]`` by the real ``LLMClient.ensure_system_prompt``
    (``agent/core/llm_client.py:222-227``);
  * the tool call is dispatched by the real
    ``ToolExecutor._execute_single_tool`` -- the function containing the
    ``SummarizeTool`` branch at ``agent/core/tool_executor.py:704-705`` -- which
    constructs and runs the real ``SummarizeTool.execute()``;
  * the pruning is the real ``Agent._apply_summary_pruning``
    (``agent/core/agent.py:1866-1943``), the production call site being
    ``agent/core/agent.py:1726-1728``;
  * the "next request" is the real request-time build at
    ``agent/core/agent.py:1297-1298``: ``context_builder.build(conversation)``
    followed by ``ContextBuilder._cleanup_orphaned_tool_messages``.

Two compactions live in ONE test (rather than a twice-parametrised test) because
the second must run on the state the first produced: ``user_history`` is
append-only, and the second summary changes which summary is "latest", i.e. which
region of history the context builder replays.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.llm_client import ADDENDUM_HEADING
from session.context_builder import ContextBuilder
from session.models import Session, normalize_addendum
from tools.summarize_tool import SummarizeTool


TEXT_ONE = 'Prefer terse answers while the migration is running.'
TEXT_TWO = 'The repository is frozen until Friday; do not open pull requests.'

SUMMARY_ONE = 'The user asked about steps 0 to 5; each step was confirmed done.'
SUMMARY_TWO = 'The user asked about steps 0 to 7; steps 6 and 7 were confirmed done.'

TURNS_BEFORE_FIRST_COMPACTION = 6
TURNS_BETWEEN_COMPACTIONS = 2
KEEP_RECENT_TURNS = 5


# ---------------------------------------------------------------------------
# Real session + real addendum (slice-1 shape, via the slice-1 normaliser)
# ---------------------------------------------------------------------------

def _entry(entry_id, text, ts='2025-01-01T00:00:00Z', source='operator', pinned=False):
    return {'id': entry_id, 'ts': ts, 'source': source, 'text': text, 'pinned': pinned}


def _addendum(entries, budget_chars=8000, max_entries=64):
    return {'entries': list(entries), 'budget_chars': budget_chars, 'max_entries': max_entries}


TWO_ENTRIES = [
    _entry('e-001', TEXT_ONE, ts='2025-03-02T09:00:00Z', source='operator', pinned=False),
    _entry('e-002', TEXT_TWO, ts='2025-01-05T17:30:00Z', source='mailbox', pinned=True),
]


def _turn(index):
    return [
        {'role': 'user', 'content': f'Question {index}: what is the status of step {index}?'},
        {'role': 'assistant', 'content': f'Answer {index}: step {index} is done.'},
    ]


def _make_session(turns):
    history = []
    for index in range(turns):
        history.extend(_turn(index))
    return Session(
        session_id='slice3-addendum-survival',
        user_history=history,
        addendum=normalize_addendum(_addendum(TWO_ENTRIES)),
    )


# ---------------------------------------------------------------------------
# The production path, driven for real
# ---------------------------------------------------------------------------

def _make_agent(session):
    config = AgentConfig(api_key='test-key', enable_logging=False)
    return Agent(config=config, session=session)


def _run_real_compaction(agent, session, summary_text, keep_recent_turns=KEEP_RECENT_TURNS):
    """Drive one real SummarizeTool compaction; return the next-request context."""
    dispatch = agent.tool_executor._execute_single_tool(
        SummarizeTool,
        {'summary': summary_text, 'keep_recent_turns': keep_recent_turns},
        'SummarizeTool',
        0,
        None,  # get_summary_requested   -- unused by this executor method
        None,  # get_summary_text
        None,  # get_summary_keep_recent_turns
        session_id=session.session_id,
    )
    assert dispatch['tool_type'] == 'summary', (
        f'the tool call never reached the SummarizeTool branch: {dispatch!r}'
    )
    # agent/core/agent.py:1726-1728 -- the production call site.
    agent._apply_summary_pruning(dispatch['summary_text'], dispatch['summary_keep_recent_turns'])
    return _next_request_context(agent)


def _next_request_context(agent):
    """agent/core/agent.py:1297-1298 -- what the next LLM request is built from."""
    messages = agent.context_builder.build(agent.conversation)
    return ContextBuilder._cleanup_orphaned_tool_messages(messages)


def _texts(messages):
    return [str(message.get('content', '')) for message in messages]


def _assert_compaction_happened(context, agent, stage):
    summaries = [m for m in context
                 if str(m.get('content', '')).startswith('Summary of previous conversation:')]
    assert len(summaries) == 1, f'{stage}: expected exactly one replayed summary, got {len(summaries)}'
    assert len(context) < len(agent.conversation), (
        f'{stage}: the next request ({len(context)} msgs) is not shorter than the '
        f'append-only history ({len(agent.conversation)} msgs) -- nothing was pruned'
    )
    assert 'Question 0' not in ' '.join(_texts(context)), (
        f'{stage}: the oldest turn was replayed, so the compaction did not take effect'
    )


def _assert_addendum_survives(context, agent, stage):
    """The addendum is in the next request exactly once, in the main prompt."""
    # The base prompt the agent really configured (resources/default_system_prompt.txt),
    # not a stub: the assertion below is that the addendum is *appended* to it.
    base_prompt = agent.config.system_prompt
    carriers = [m for m in context if ADDENDUM_HEADING in str(m.get('content', ''))]
    assert len(carriers) == 1, (
        f'{stage}: the addendum heading appears in {len(carriers)} context messages, expected 1'
    )
    main_prompt = carriers[0]
    assert main_prompt.get('role') == 'system', f'{stage}: the main prompt is not a system message'
    assert context[0].get('content') == main_prompt.get('content'), (
        f'{stage}: the addendum does not ride in the message at position 0'
    )
    content = main_prompt['content']
    assert base_prompt, f'{stage}: the agent has no base system prompt to append to'
    assert content.startswith(base_prompt), f'{stage}: the base prompt prefix was lost'
    assert content[len(base_prompt):].startswith(f'\n\n{ADDENDUM_HEADING}'), (
        f'{stage}: the addendum is not appended to the end of the base prompt'
    )
    assert content.count(ADDENDUM_HEADING) == 1, f'{stage}: the addendum heading was duplicated'
    assert content.count(TEXT_ONE) == 1, f'{stage}: entry 1 rendered {content.count(TEXT_ONE)} times'
    assert content.count(TEXT_TWO) == 1, f'{stage}: entry 2 rendered {content.count(TEXT_TWO)} times'
    for message in context[1:]:
        body = str(message.get('content', ''))
        assert TEXT_ONE not in body, f'{stage}: entry 1 leaked into a non-prompt message'
        assert TEXT_TWO not in body, f'{stage}: entry 2 leaked into a non-prompt message'


# ---------------------------------------------------------------------------
# Slice 3: two real compactions, addendum still present in the next request
# ---------------------------------------------------------------------------

def test_addendum_survives_two_real_summarize_tool_compactions():
    session = _make_session(TURNS_BEFORE_FIRST_COMPACTION)
    agent = _make_agent(session)

    # The addendum reached the system prompt at construction time (slice 2).
    _assert_addendum_survives(_next_request_context(agent), agent, 'before any compaction')

    # --- Compaction 1 -------------------------------------------------------
    context = _run_real_compaction(agent, session, SUMMARY_ONE)
    _assert_compaction_happened(context, agent, 'after compaction 1')
    _assert_addendum_survives(context, agent, 'after compaction 1')

    # The conversation continues after the compaction (append-only history).
    for index in range(TURNS_BEFORE_FIRST_COMPACTION,
                       TURNS_BEFORE_FIRST_COMPACTION + TURNS_BETWEEN_COMPACTIONS):
        for message in _turn(index):
            agent.conversation.append(message)

    # --- Compaction 2 -------------------------------------------------------
    context = _run_real_compaction(agent, session, SUMMARY_TWO)
    _assert_compaction_happened(context, agent, 'after compaction 2')
    _assert_addendum_survives(context, agent, 'after compaction 2')

    # The second compaction replaced the replayed summary (latest wins), so this
    # really was a second compaction and not a replay of the first one.
    replayed = next(m['content'] for m in context
                    if str(m.get('content', '')).startswith('Summary of previous conversation:'))
    assert SUMMARY_TWO in replayed
    assert SUMMARY_ONE not in replayed
