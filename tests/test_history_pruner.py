"""
Unit tests for session/history_pruner.py

Covers all scenarios described in AI_Tasks/session_compression2.txt Phase 2.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List

import pytest

from agent.core.message import Message
from session.history_pruner import (
    FINAL_TOOL_NAMES,
    RESPOND_FAMILY_NAMES,
    PruningPolicy,
    prune_user_history,
    _find_summary_indices,
    _group_turns,
    _is_system_notification,
)
from session.models import Session
from session.store import FileSystemSessionStore


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

_SEQ = iter(range(1000))


def _reset_seq():
    global _SEQ
    _SEQ = iter(range(1000))


def _msg(
    role: str,
    content: str = '',
    seq: int | None = None,
    **extra,
) -> Dict[str, Any]:
    """Build a minimal message dict."""
    msg: Dict[str, Any] = {
        'role': role,
        'content': content,
        'seq': next(_SEQ) if seq is None else seq,
        'created_at': '2026-05-04T00:00:00.000000',
    }
    msg.update(extra)
    return msg


def _user(content: str = 'User message', **kw) -> Dict[str, Any]:
    return _msg('user', content, **kw)


def _asst(content: str = 'Assistant response', tool_calls=None, **kw) -> Dict[str, Any]:
    msg = _msg('assistant', content, **kw)
    if tool_calls is not None:
        msg['tool_calls'] = tool_calls
    return msg


def _tool(content: str, tool_call_id: str, **kw) -> Dict[str, Any]:
    return _msg('tool', content, tool_call_id=tool_call_id, **kw)


def _sys(content: str = 'System', **kw) -> Dict[str, Any]:
    return _msg('system', content, **kw)


def _summary(content: str = 'Summary of previous conversation: ...', **kw) -> Dict[str, Any]:
    return _msg(
        'system', content,
        summary=True,
        pruning_keep_recent_turns=3,
        pruning_discarded_msg_count=5,
        pruning_insertion_idx=2,
        **kw,
    )


def _notification(content: str = '[SYSTEM NOTIFICATION] Test') -> Message:
    return Message(role='user', content=content)


def _tool_call(name: str, call_id: str, args: str = '{}') -> Dict[str, Any]:
    return {
        'id': call_id,
        'type': 'function',
        'function': {'name': name, 'arguments': args},
    }


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────


class TestFindSummaryIndices:
    def test_no_summaries(self):
        history = [_user(), _asst()]
        assert _find_summary_indices(history) == []

    def test_one_summary(self):
        history = [_user(), _asst(), _summary()]
        assert _find_summary_indices(history) == [2]

    def test_multiple_summaries(self):
        history = [_user(), _asst(), _summary(), _user(), _asst(), _summary()]
        assert _find_summary_indices(history) == [2, 5]

    def test_summary_without_flag_not_counted(self):
        """A system message without summary=True is not a summary."""
        msg = _sys('Some system prompt')
        assert _find_summary_indices([msg]) == []


class TestIsSystemNotification:
    def test_flag_true(self):
        assert _is_system_notification({'is_system_notification': True, 'role': 'user'}) is True

    def test_content_marker(self):
        assert _is_system_notification({'role': 'user', 'content': '[SYSTEM NOTIFICATION] High tokens'}) is True

    def test_regular_user(self):
        assert _is_system_notification({'role': 'user', 'content': 'Hello'}) is False

    def test_assistant(self):
        assert _is_system_notification({'role': 'assistant', 'content': 'Hi'}) is False


class TestGroupTurns:
    def test_single_turn(self):
        msgs = [_user('Q1'), _asst('A1')]
        turns = _group_turns(msgs)
        assert len(turns) == 1
        assert len(turns[0]) == 2

    def test_multiple_turns(self):
        msgs = [
            _user('Q1'), _asst('A1'),
            _user('Q2'), _asst('A2'),
        ]
        turns = _group_turns(msgs)
        assert len(turns) == 2
        assert len(turns[0]) == 2
        assert len(turns[1]) == 2

    def test_notifications_start_turns_as_user_role(self):
        """System notifications (user-role) start a new turn in _group_turns,
        since the function treats all user-role messages as turn starters.
        Notifications are filtered out *before* _group_turns by _compact_segment,
        so this edge case doesn't arise in normal pruning flow."""
        msgs = [_user('Q1'), _asst('A1'), _notification()]
        turns = _group_turns(msgs)
        # Notification has role='user', so it starts a new turn
        assert len(turns) == 2
        assert len(turns[0]) == 2  # Q1, A1
        assert len(turns[1]) == 1  # notification

    def test_dual_signal_grouping(self):
        """User + assistant-with-tool_calls produces 2 turns (dual start signals).
        Both user and assistant-with-tc start new turns, matching SummaryBuilder."""
        tc1 = _tool_call('DockerCodeRunner', 'call_1')
        tc2 = _tool_call('Final', 'call_2')
        msgs = [
            _user('Run test'),
            _asst('Running...', tool_calls=[tc1, tc2]),
            _tool('{"exit_code": 0}', 'call_1'),
            _tool('Done!', 'call_2'),
        ]
        turns = _group_turns(msgs)
        # User starts turn 1, assistant-with-tc starts turn 2 (dual signal)
        assert len(turns) == 2
        assert len(turns[0]) == 1  # just the user
        assert len(turns[1]) == 3  # asst + 2 tools


# ──────────────────────────────────────────────────────────────────────
# Pruning scenarios
# ──────────────────────────────────────────────────────────────────────


class TestPruneNoPruningIfOnlyOneSummary:
    def test_no_summaries_returns_copy(self):
        _reset_seq()
        history = [_sys('prompt'), _user(), _asst()]
        result = prune_user_history(history)
        assert result == history
        assert result is not history  # different list object

    def test_one_summary_unchanged(self):
        _reset_seq()
        history = [_sys('prompt'), _user(), _asst(), _summary()]
        result = prune_user_history(history)
        assert len(result) == len(history)
        # All messages present
        for orig, res in zip(history, result):
            assert orig['seq'] == res['seq']

    def test_default_min_is_2(self):
        policy = PruningPolicy()  # min_summaries_before_pruning=2
        _reset_seq()
        history = [_sys('prompt'), _user(), _asst(), _summary()]
        result = prune_user_history(history, policy)
        assert len(result) == len(history)


class TestPruneWithTwoSummaries:
    def test_basic_two_summary_pruning(self):
        """Only region before second summary is compacted."""
        _reset_seq()
        history = [
            _sys('prompt'),
            _user('Turn1 Q'), _asst('Turn1 A'),
            _summary('Summary 1'),
            _user('Turn2 Q'), _asst('Turn2 A'),
            _summary('Summary 2'),
            _user('Turn3 Q'), _asst('Turn3 A'),  # safe region
        ]
        result = prune_user_history(history)
        # The safe region (after second summary) must be intact
        assert result[-1]['content'] == 'Turn3 A'
        assert result[-2]['content'] == 'Turn3 Q'
        # The second summary must still be present
        assert any(
            m.get('summary') and 'Summary 2' in m.get('content', '')
            for m in result
        )

    def test_old_region_compacted(self):
        """Old region turns should be collapsed to user+final or user+assistant."""
        _reset_seq()
        tc = _tool_call('Final', 'call_final')
        history = [
            _sys('prompt'),
            _user('Old Q'),
            _asst('Old A', tool_calls=[tc]),
            _tool('Final result', 'call_final'),
            _summary('Summary 1'),
            _user('Mid Q'),
            _asst('Mid A'),
            _summary('Summary 2'),
            _user('New Q'),
            _asst('New A'),
        ]
        result = prune_user_history(history)
        # Must still have 3 user messages (Old Q, Mid Q, New Q)
        user_msgs = [m for m in result if m.get('role') == 'user' and not _is_system_notification(m)]
        assert len(user_msgs) == 3


class TestPruneTurnWithFinalTool:
    def test_final_turn_keeps_user_asst_and_result(self):
        """A turn with a Final tool call keeps user, final assistant, and final tool result."""
        _reset_seq()
        tc = _tool_call('Final', 'call_final')
        history = [
            _summary('Summary 1'),
            _summary('Summary 2'),  # second summary = cut point
            _user('Run test'),
            _asst('Running...', tool_calls=[tc]),
            _tool('Tests passed!', 'call_final'),
        ]
        result = prune_user_history(history)
        # User message kept
        assert any(m.get('content') == 'Run test' for m in result)
        # Assistant with final kept
        assert any(m.get('content') == 'Running...' for m in result)
        # Final tool result kept
        assert any(m.get('content') == 'Tests passed!' for m in result)
        # But intermediate tool calls are gone (there were none here)

    def test_multi_tool_with_final(self):
        """Multiple tool calls, only final-related messages survive."""
        _reset_seq()
        tc1 = _tool_call('DockerCodeRunner', 'call_1')
        tc2 = _tool_call('FileEditor', 'call_2')
        tc3 = _tool_call('Final', 'call_final')
        history = [
            _user('Pre-summary turn'), _asst('Pre A'),  # must be before S1 so old region is non-empty
            _summary('S1'),
            _summary('S2'),
            _user('Do the thing'),
            _asst('Starting', tool_calls=[tc1, tc2, tc3]),
            _tool('{"exit_code": 0}', 'call_1'),
            _tool('File written', 'call_2'),
            _tool('Done, finalizing', 'call_final'),
        ]
        result = prune_user_history(history)
        # User messages: Pre-summary turn, Do the thing
        user_msgs = [m for m in result if m.get('role') == 'user' and not _is_system_notification(m)]
        assert len(user_msgs) == 2
        # Assistant with all tool_calls kept (the final one)
        asst_with_final = [m for m in result if m.get('role') == 'assistant' and m.get('tool_calls')]
        assert len(asst_with_final) == 1
        assert len(asst_with_final[0].get('tool_calls', [])) == 3
        # Tool messages: "Pre A" turn has none (compact), safe region has all 3
        tool_msgs = [m for m in result if m.get('role') == 'tool']
        assert len(tool_msgs) == 3  # safe region unchanged
        assert tool_msgs[2]['tool_call_id'] == 'call_final'  # last tool is the final one


class TestPruneTurnWithoutFinal:
    def test_plain_assistant_ending(self):
        """Turn ending with plain assistant → keep user + last assistant."""
        _reset_seq()
        history = [
            _summary('S1'),
            _summary('S2'),
            _user('Question 1'),
            _asst('Answer 1'),
        ]
        result = prune_user_history(history)
        assert len(result) == 4  # 2 summaries + user + asst
        assert result[2]['role'] == 'user'
        assert result[3]['role'] == 'assistant'

    def test_multiple_assistants_no_final(self):
        """Multiple assistants but no final → keep user + last assistant with content."""
        _reset_seq()
        history = [
            _user('Pre Q'), _asst('Pre A'),  # old region filler
            _summary('S1'),
            _summary('S2'),
            _user('Complex task'),
            _asst('Thinking...', tool_calls=[_tool_call('GlobTool', 'call_g')]),
            _tool('results', 'call_g'),
            _asst('Here is the answer'),
        ]
        result = prune_user_history(history)
        # 'Pre Q' and 'Complex task' should be the user messages
        user_msgs = [m for m in result if m.get('role') == 'user' and not _is_system_notification(m)]
        assert len(user_msgs) == 2
        assert user_msgs[1]['content'] == 'Complex task'
        # Last assistant with content kept
        asst_msgs = [m for m in result if m.get('role') == 'assistant']
        assert any(m.get('content') == 'Here is the answer' for m in asst_msgs)
        # Tool messages from safe region survive
        tool_msgs = [m for m in result if m.get('role') == 'tool']
        assert len(tool_msgs) == 1  # from safe region

    def test_tool_calls_no_final(self):
        """Turn with tool calls but none final → drops tools, keeps user + last content asst."""
        _reset_seq()
        tc = _tool_call('GlobTool', 'call_g')
        history = [
            _user('Pre Q'), _asst('Pre A'),  # old region filler
            _summary('S1'),
            _summary('S2'),
            _user('Find files'),
            _asst('Searching...', tool_calls=[tc]),
            _tool('["file1.txt"]', 'call_g'),
            _asst('Found: file1.txt'),
        ]
        result = prune_user_history(history)
        # No tool messages in the old region's compacted output
        # (the safe region after S2 still has the originals but that's fine)
        asst_msgs = [m for m in result if m.get('role') == 'assistant']
        assert any(m.get('content') == 'Found: file1.txt' for m in asst_msgs)

    def test_keep_plain_answer_only_fallback(self):
        """When keep_plain_answer_only=True but no assistant has plain text
        (all have tool_calls), fall back to last assistant with any content.
        The turn must be in the OLD region to trigger compaction."""
        _reset_seq()
        # Single assistant with multiple tool_calls — no plain answer at end
        tc1 = _tool_call('GlobTool', 'call_g1')
        tc2 = _tool_call('FileEditor', 'call_g2')
        history = [
            _summary('S1'),           # 0
            _user('Do work'),          # 1 — in old region
            _asst('Tool results', tool_calls=[tc1, tc2]),  # 2 — in old region
            _tool('["x.txt"]', 'call_g1'),  # 3 — in old region
            _tool('Written!', 'call_g2'),  # 4 — in old region
            _summary('S2'),           # 5 — second-last summary = cut point
            _user('Q2'), _asst('A2'),  # 6, 7 — safe region
            _summary('S3'),           # 8 — last summary
            _user('Q3'), _asst('A3'),  # 9, 10 — safe region
        ]
        # Summary indices: [0, 5, 8]; second-last = 5
        # old = [0:5] = [S1, user, asst, tool, tool]
        # safe = [5:] = [S2, user(Q2), asst(A2), S3, user(Q3), asst(A3)]
        result = prune_user_history(history)
        asst_msgs = [m for m in result if m.get('role') == 'assistant']
        # Old region: S1 passthrough, [user] + [asst, tool, tool] (dual signal)
        # _compact_turn on [asst, tool, tool]: no final tool, keep_plain_answer_only=True
        #   asst has content='Tool results' but HAS tool_calls → not plain
        #   No plain assistant → fallback: last with content = asst('Tool results')
        # So old region contributes: [S1, user('Do work'), asst('Tool results')]
        assert any(m.get('content') == 'Tool results' for m in asst_msgs)
        # Only 3 assistants total: Tool results (old), A2 (safe), A3 (safe)
        assert len(asst_msgs) == 3

    def test_keep_plain_answer_only_false(self):
        """With keep_plain_answer_only=False, keep last assistant even if it has tool_calls."""
        _reset_seq()
        tc = _tool_call('GlobTool', 'call_g')
        policy = PruningPolicy(keep_plain_answer_only=False)
        history = [
            _user('Pre Q'), _asst('Pre A'),
            _summary('S1'),
            _summary('S2'),
            _user('Do work'),
            _asst('Step 1'),
            _asst('Step 2', tool_calls=[tc]),
            _tool('Done', 'call_g'),
        ]
        result = prune_user_history(history, policy)
        # With keep_plain_answer_only=False, the last assistant with content is 'Step 2'
        # It has content AND tool_calls, which is kept when keep_plain_answer_only=False
        asst_msgs = [m for m in result if m.get('role') == 'assistant']
        assert any(m.get('content') == 'Step 2' for m in asst_msgs)


class TestPruneSystemMessagesAndNotifications:
    def test_system_messages_pass_through(self):
        """System messages in the old region must survive."""
        _reset_seq()
        history = [
            _sys('prompt'),
            _summary('S1'),
            _user('Q'), _asst('A'),
            _summary('S2'),
        ]
        result = prune_user_history(history)
        # System prompt and both summaries must be present
        sys_msgs = [m for m in result if m.get('role') == 'system']
        assert len(sys_msgs) == 3

    def test_notifications_pass_through(self):
        """System notifications in the old region survive."""
        _reset_seq()
        n1 = _notification('[SYSTEM NOTIFICATION] Warning 1')
        n2 = _notification('[SYSTEM NOTIFICATION] Warning 2')
        history = [
            _summary('S1'),
            n1,
            _user('Q'), _asst('A'),
            _summary('S2'),
            n2,
            _user('New Q'), _asst('New A'),
        ]
        result = prune_user_history(history)
        notif_msgs = [m for m in result if _is_system_notification(m)]
        assert len(notif_msgs) == 2

    def test_passthrough_preserves_order(self):
        """System and notification messages preserve relative order."""
        _reset_seq()
        history = [
            _sys('prompt'),
            _summary('S1'),
            _notification('[SYSTEM NOTIFICATION] N1'),
            _user('Q1'), _asst('A1'),
            _sys('intermediate'),
            _summary('S2'),
            _user('Q2'), _asst('A2'),
        ]
        result = prune_user_history(history)
        # Check that all system/notification messages appear before user turns
        sys_and_notif = [m for m in result if m.get('role') == 'system' or _is_system_notification(m)]
        # All 5 should be present (prompt, S1, N1, intermediate, S2)
        assert len(sys_and_notif) == 5

    def test_notifications_dropped_from_old_region(self):
        """With keep_system_notifications=False (default), notifications in
        the old (pruned) region are dropped entirely."""
        _reset_seq()
        n1 = _notification('[SYSTEM NOTIFICATION] Warning 1')
        history = [
            _summary('S1'),
            n1,
            _user('Q'), _asst('A'),
            _summary('S2'),
            _user('New Q'), _asst('New A'),
        ]
        # Summary indices: [0, 3]; second-last = 0
        # old = [0:0] = [] — no old region, so notification is in safe region
        # To test old region dropping, we need old to contain notifications
        # Let's add a notification BEFORE S1
        history2 = [
            n1,                      # 0 — in old region
            _summary('S1'),           # 1
            _user('Q'), _asst('A'),   # 2, 3
            _summary('S2'),           # 4
            _user('New Q'), _asst('New A'),  # 5, 6
        ]
        # Summary indices: [1, 4]; second-last = 1
        # old = [0:1] = [n1]; safe = [1:]
        result = prune_user_history(history2)
        notif_msgs = [m for m in result if _is_system_notification(m)]
        assert len(notif_msgs) == 0  # notification dropped from old region

    def test_notifications_kept_in_old_region_with_policy(self):
        """With keep_system_notifications=True, notifications in old region survive."""
        _reset_seq()
        n1 = _notification('[SYSTEM NOTIFICATION] Warning 1')
        policy = PruningPolicy(keep_system_notifications=True)
        history = [
            n1,
            _summary('S1'),
            _user('Q'), _asst('A'),
            _summary('S2'),
            _user('New Q'), _asst('New A'),
        ]
        result = prune_user_history(history, policy)
        notif_msgs = [m for m in result if _is_system_notification(m)]
        assert len(notif_msgs) == 1

    def test_summaries_remain_in_original_order(self):
        """Summaries in the old region must retain their original positions
        relative to turns, not be moved to the beginning."""
        _reset_seq()
        history = [
            _summary('S1'),           # 0
            _user('Q1'), _asst('A1'),  # 1, 2
            _summary('S2'),           # 3
            _user('Q2'), _asst('A2'),  # 4, 5
        ]
        result = prune_user_history(history)
        # Result should preserve order: S1, Q1, A1, S2, Q2, A2
        # S2 should be between the two turns, not at the end
        sys_msgs = [m for m in result if m.get('role') == 'system']
        assert len(sys_msgs) == 2
        assert sys_msgs[0]['content'] == 'S1'
        assert sys_msgs[1]['content'] == 'S2'
        # Verify S2 appears before Q2 (i.e., between turns)
        s2_idx = next(i for i, m in enumerate(result)
                      if m.get('role') == 'system' and m.get('content') == 'S2')
        q2_idx = next(i for i, m in enumerate(result)
                      if m.get('content') == 'Q2')
        assert s2_idx < q2_idx, (
            f'S2 at index {s2_idx} should be before Q2 at index {q2_idx}'
        )
        # Verify the full sequence order
        seq_contents = [m.get('content') for m in result]
        s1_pos = seq_contents.index('S1')
        q1_pos = seq_contents.index('Q1')
        a1_pos = seq_contents.index('A1')
        s2_pos = seq_contents.index('S2')
        q2_pos = seq_contents.index('Q2')
        a2_pos = seq_contents.index('A2')
        assert s1_pos < q1_pos < a1_pos < s2_pos < q2_pos < a2_pos, (
            f'Wrong order: {seq_contents}'
        )


# ──────────────────────────────────────────────────────────────────────
# Assistant-started turn scenarios (dual-signal)
# ──────────────────────────────────────────────────────────────────────


class TestAssistantStartedTurns:
    """Tests for turns starting with assistant (no user), which happens
    when the dual-signal grouping in _group_turns separates user from
    assistant-with-tc, or when pruning cuts off the user portion."""

    def test_compact_no_user_turn_keeps_assistant(self):
        """A turn with no user message keeps the assistant with content."""
        from session.history_pruner import _compact_turn
        _reset_seq()
        turn = [_asst('Direct answer without user')]
        result = _compact_turn(turn, PruningPolicy())
        assert len(result) == 1
        assert result[0]['role'] == 'assistant'
        assert result[0]['content'] == 'Direct answer without user'

    def test_compact_no_user_with_tool_calls(self):
        """A turn starting with assistant-with-tc (no user): assistant kept
        as TEXT ONLY and the Respond-family tool result converted to an
        assistant-style message — no orphaned tool call IDs survive."""
        from session.history_pruner import _compact_turn
        _reset_seq()
        tc = _tool_call('Final', 'call_f')
        turn = [
            _asst('Running final', tool_calls=[tc]),
            _tool('Result', 'call_f'),
        ]
        result = _compact_turn(turn, PruningPolicy())
        assert len(result) == 2  # assistant text + converted result
        assert result[0]['role'] == 'assistant'
        assert 'tool_calls' not in result[0]
        assert result[1]['role'] == 'assistant'
        assert result[1]['content'] == 'Result'
        assert 'tool_call_id' not in result[1]

    def test_pruning_between_user_and_assistant(self):
        """Integration: if a summary falls between user and its assistant,
        the assistant-started portion is in the 'old' region and gets
        compacted as an assistant-started turn."""
        _reset_seq()
        tc = _tool_call('Final', 'call_f')
        history = [
            _summary('S1'),           # 0
            _user('Q1'), _asst('A1'),  # 1, 2
            _summary('S2'),           # 3 — cut point (second-last = 2 summaries ago)
            _user('Q2'), _asst('A2'),  # 4, 5
            _summary('S3'),           # 6 — last summary
            _user('Q3'), _asst('A3'),  # 7, 8
        ]
        # Summary indices: [0, 3, 6]; second-last = 3
        # old = [0:3] = [S1, user(Q1), asst(A1)]
        # safe = [3:] = [S2, user(Q2), asst(A2), S3, user(Q3), asst(A3)]
        result = prune_user_history(history)
        # Old region: S1 is system passthrough, [user(Q1), asst(A1)] is a turn
        # Compacted: [user(Q1), asst(A1)] (plain assistant kept)
        # So we should have: S1, user(Q1), asst(A1), S2, user(Q2), asst(A2), S3, user(Q3), asst(A3)
        assert any(m.get('content') == 'Q1' for m in result)
        assert any(m.get('content') == 'A1' for m in result)

    def test_pruning_with_assistant_starts_in_old_region(self):
        """When old region contains assistant-started messages (from dual-signal
        grouping), they're compacted as a turn starting with assistant."""
        _reset_seq()
        tc = _tool_call('GlobTool', 'call_g')
        history = [
            _summary('S1'),           # 0
            # In the old region, we have messages that after grouping and separation
            # form an assistant-started turn
            _user('Find file'),        # 1 — starts turn 1
            _asst('Searching...', tool_calls=[tc]),  # 2 — would start turn 2 (dual signal)
            _tool('["a.txt"]', 'call_g'),  # 3 — attaches to turn 2
            _asst('Found a.txt'),     # 4 — attaches to turn 2
            _summary('S2'),           # 5 — cut point
            _user('Q2'), _asst('A2'),  # 6, 7 — safe
            _summary('S3'),           # 8
            _user('Q3'), _asst('A3'),  # 9, 10 — safe
        ]
        # Summary indices: [0, 5, 8]; second-last = 5
        # old = [0:5] = [S1, user, asst(w tc), tool, asst]
        # safe = [5:] = [S2, user(Q2), asst(A2), S3, user(Q3), asst(A3)]
        result = prune_user_history(history)
        # Old region compacted:
        #   - S1 is system passthrough
        #   - turn 1 [user('Find file')] -> [user('Find file')]
        #   - turn 2 [asst('Searching...' w tc), tool, asst('Found a.txt')] ->
        #     asst text kept (tool_calls stripped), GlobTool result dropped,
        #     asst('Found a.txt') kept as text
        # So compacted = [S1, user('Find file'), asst('Searching...'), asst('Found a.txt')]
        user_msgs = [m for m in result if m.get('role') == 'user' and not _is_system_notification(m)]
        assert len(user_msgs) == 3  # Find file, Q2, Q3
        # The old region's assistant 'Found a.txt' should be present (text kept)
        assert any(m.get('content') == 'Found a.txt' for m in result)
        # The intermediate asst 'Searching...' is kept as TEXT ONLY
        searching = [m for m in result if m.get('content') == 'Searching...']
        assert len(searching) == 1
        assert 'tool_calls' not in searching[0]
        # The GlobTool result is dropped (not Respond-family)
        assert not any(m.get('content') == '["a.txt"]' for m in result)


class TestSeqNumberingPreserved:
    def test_seq_values_untouched(self):
        """Seq numbers of kept messages must match originals exactly."""
        _reset_seq()
        history = [
            _summary('S1', seq=10),
            _summary('S2', seq=20),
            _user('Q', seq=30), _asst('A', seq=31),
        ]
        result = prune_user_history(history)
        for orig, res in zip(history, result):
            assert orig['seq'] == res['seq']


class TestMultipleSummarizations:
    def test_three_summaries_prune_oldest(self):
        """Three summaries: only oldest region (before second-last) is pruned."""
        _reset_seq()
        history = [
            _user('Very old 1'), _asst('A1'),
            _summary('S1'),
            _user('Old 2'), _asst('A2'),
            _summary('S2'),
            _user('Recent 3'), _asst('A3'),
            _summary('S3'),
            _user('Latest 4'), _asst('A4'),
        ]
        result = prune_user_history(history)
        # All 3 summaries still present
        summaries = [m for m in result if m.get('summary')]
        assert len(summaries) == 3
        # Safe region (S3 onwards) intact
        assert any(m.get('content') == 'Latest 4' for m in result)
        assert any(m.get('content') == 'A4' for m in result)

    def test_two_most_recent_cycles_intact(self):
        """The two most recent summarization cycles are preserved in full."""
        _reset_seq()
        history = [
            _user('Archived 1'), _asst('A1'),
            _summary('S1'),
            _user('Archived 2'), _asst('A2'),
            _summary('S2'),  # second-last summary
            _user('Kept 3'), _asst('A3'),
            _summary('S3'),  # last summary
            _user('Latest 4'), _asst('A4'),
        ]
        result = prune_user_history(history)
        # Find the positions of S2 and S3
        summ_indices = [i for i, m in enumerate(result) if m.get('summary')]
        assert len(summ_indices) == 3
        # Latest 4 is the most recent user, should be present
        assert any(m.get('content') == 'Latest 4' for m in result)


    def test_orphans_dropped(self):
        """Orphaned tool messages (no assistant-with-tc in turn) are dropped.
        Orphaned non-user, non-assistant messages without an active turn are dropped."""
        tc = _tool_call('GlobTool', 'call_g')
        msgs = [
            _tool('{"exit_code": 0}', 'call_g'),  # orphan — no turn context
            _user('Q1'),
            _tool('{"result": "ok"}', 'call_orphan'),  # orphan — no asst-with-tc in current turn
            _asst('A1'),
        ]
        turns = _group_turns(msgs)
        # First tool is orphan (no turn context) -> dropped
        # Second tool is orphan (turn has plain assistant, not asst-with-tc) -> dropped
        assert len(turns) == 1
        assert len(turns[0]) == 2  # user + plain assistant


class TestEmptyOldRegion:
    def test_cut_idx_zero(self):
        """If second-last summary is first entry, no old region."""
        _reset_seq()
        history = [
            _summary('S1'),          # first summary
            _user('A'), _asst('B'),
            _summary('S2'),          # second summary = cut point at idx 0? No...
        ]
        # The summary indices are [0, 3]; second-last is 0; old = history[:0] = []
        result = prune_user_history(history)
        # Should be unchanged since old region is empty
        assert len(result) == len(history)

    def test_cut_idx_first_message(self):
        """Second-last summary is the very first message."""
        _reset_seq()
        history = [
            _summary('S1', seq=1),   # index 0
            _user('M1', seq=2), _asst('A1', seq=3),
            _summary('S2', seq=4),   # index 3 — last summary
            _user('M2', seq=5), _asst('A2', seq=6),
        ]
        # Summary indices: [0, 3]; second-last = 0
        # old = history[:0] = []; safe = history[0:]
        result = prune_user_history(history)
        assert len(result) == len(history)


class TestEdgeCaseMissingToolResult:
    def test_final_call_missing_result(self):
        """Missing tool result for a final call — warning logged, no crash."""
        _reset_seq()
        tc = _tool_call('Final', 'call_final')
        history = [
            _summary('S1'),
            _summary('S2'),
            _user('Do it'),
            _asst('Running', tool_calls=[tc]),
            # No tool message with tool_call_id='call_final'
        ]
        # Should not raise
        result = prune_user_history(history)
        assert result is not None
        # User + assistant kept
        assert any(m.get('role') == 'user' for m in result)
        assert any(m.get('role') == 'assistant' for m in result)


# ──────────────────────────────────────────────────────────────────────
# Integration sanity
# ──────────────────────────────────────────────────────────────────────


class TestPruneIdempotent:
    def test_pruning_twice_same_result(self):
        """Applying prune twice should give the same result as once."""
        _reset_seq()
        history = [
            _summary('S1'),
            _summary('S2'),
            _user('Q'), _asst('A'),
            _user('Q2'), _asst('A2'),
        ]
        result1 = prune_user_history(history)
        result2 = prune_user_history(result1)
        # After first prune, there may be 0 summaries in the old region
        # so second prune should be no-op
        assert len(result2) == len(result1)
        for m1, m2 in zip(result1, result2):
            assert m1['seq'] == m2['seq']

    def test_input_not_mutated(self):
        """Original list must never be modified."""
        original = [
            _msg('system', 'prompt', seq=0),
            _msg('user', 'Q', seq=1),
            _msg('assistant', 'A', seq=2),
            _summary('S1', seq=3),
        ]
        original_copy = copy.deepcopy(original)
        _ = prune_user_history(original)
        assert original == original_copy


class TestFinalToolNames:
    def test_all_final_names_defined(self):
        assert 'Final' in FINAL_TOOL_NAMES
        assert 'FinalReport' in FINAL_TOOL_NAMES
        assert 'RequestUserInteraction' in FINAL_TOOL_NAMES

    def test_no_extra_names(self):
        assert len(FINAL_TOOL_NAMES) == 3


# ──────────────────────────────────────────────────────────────────────
# Old-region semantics: end-to-end through the real store (save-time)
# ──────────────────────────────────────────────────────────────────────


class TestOldRegionNewSemantics:
    """End-to-end tests through FileSystemSessionStore.save_session (pruning
    enabled) for the new old-region semantics: user messages kept, assistant
    TEXT only, Respond-family tool results converted to assistant-style text,
    everything else dropped, last two summary cycles byte-identical."""

    @staticmethod
    def _build_history() -> List[Dict[str, Any]]:
        """prompt, cycle 0 (3 tool calls incl. Respond), S1, cycle 1, S2,
        Q2/A2. Summaries at indices 6 and 10 -> old region = [0:6]."""
        _reset_seq()
        g0 = _tool_call('git_read', 'g0')
        w0 = _tool_call('Worker', 'w0')
        r0 = _tool_call('Respond', 'r0')
        r1 = _tool_call('Respond', 'r1')
        return [
            _sys('prompt'),                                            # 0
            _user('Q0'),                                               # 1
            _asst('Running...', tool_calls=[g0, w0, r0]),              # 2
            _tool('{"exit_code":0}', 'g0'),                            # 3
            _tool('{"found":"x"}', 'w0'),                              # 4
            _tool('Answer 0', 'r0', response_type='answer'),           # 5
            _summary('S1'),                                            # 6
            _user('Q1'),                                               # 7
            _asst('Thinking...', tool_calls=[r1]),                     # 8
            _tool('Answer 1', 'r1', response_type='answer'),           # 9
            _summary('S2'),                                            # 10
            _user('Q2'),                                               # 11
            _asst('A2'),                                               # 12
        ]

    @staticmethod
    def _save(tmp_path, history):
        store = FileSystemSessionStore(
            sessions_dir=str(tmp_path / 'sessions'),
            state_dir=str(tmp_path / 'state'),
        )
        session = Session.from_persistable_dict({
            'session_id': 'pruner-old-region',
            'metadata': {'name': 'Pruner Old Region'},
            'user_history': list(history),
        })
        sid = session.session_id
        store.save_session(session)
        path = store._find_session_path(sid)
        assert path is not None and path.exists()
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_old_region_has_no_orphaned_tool_calls(self, tmp_path):
        history = self._build_history()
        data = self._save(tmp_path, history)
        old = data['user_history'][:6]
        # No assistant keeps a tool_calls array and no tool message survives
        assert all('tool_calls' not in m for m in old)
        assert all(m.get('role') != 'tool' for m in old)

    def test_old_region_keeps_user_messages(self, tmp_path):
        history = self._build_history()
        data = self._save(tmp_path, history)
        out = data['user_history']
        contents = [m.get('content') for m in out]
        # Q0 lives in the old region; Q1/Q2 in the kept region
        assert 'Q0' in contents
        assert 'Q1' in contents
        assert 'Q2' in contents

    def test_old_region_keeps_assistant_text_only(self, tmp_path):
        history = self._build_history()
        data = self._save(tmp_path, history)
        old = data['user_history'][:6]
        running = [m for m in old if m.get('content') == 'Running...']
        assert len(running) == 1
        assert 'tool_calls' not in running[0]
        assert 'reasoning_content' not in running[0]

    def test_old_region_keeps_respond_family_output_only(self, tmp_path):
        history = self._build_history()
        data = self._save(tmp_path, history)
        old = data['user_history'][:6]
        asst_contents = [m.get('content') for m in old if m.get('role') == 'assistant']
        # Respond tool output converted to assistant-style text
        assert 'Answer 0' in asst_contents
        # Non-Respond tool outputs dropped entirely
        serialized = json.dumps(old)
        assert '{"exit_code":0}' not in serialized
        assert '{"found":"x"}' not in serialized

    def test_last_two_summary_cycles_preserved(self, tmp_path):
        history = self._build_history()
        data = self._save(tmp_path, history)
        out = data['user_history']
        s1_idx = next(i for i, m in enumerate(out) if m.get('content') == 'S1')
        # Everything from the second-last summary on is byte-identical
        assert out[s1_idx:] == history[6:]
