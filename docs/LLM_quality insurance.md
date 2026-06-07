How you could have made me warn you earlier

You can't read my mind, but you can force me to think architecturally. Here are specific prompts that would have surfaced the problem sessions ago:
1. "Before we fix the next bug, draw the full data flow for me."

This forces me to articulate the current model. When I try to draw it and find gaps or contradictions, I'll flag them. In this case, drawing the flow would have revealed: "wait, the core injects notifications into user_history, but the bridge is waiting for a token_warning event that may never come."
2. "What changed in the core that could make this hard?"

This triggers a re‑read of the core engineer's changes with the specific question: do any of these changes break assumptions the bridge relies on? I would have spotted the notification injection change and the event ordering change as high‑risk for the bridge's event‑dispatch model.
3. "Is there a simpler way to do this whole thing?"

This is the nuclear option — ask me to throw away the current approach and start fresh. I should have done this myself after the third patch. You asking it would have forced it.
4. "Rate your confidence in this fix out of 10."

If I say 7 or lower, ask "what would make it a 9?" The answer usually reveals the architectural doubt I'm suppressing.
5. "What question would the next engineer ask that we haven't?"

This surfaces the blind spots. The new engineer asked "what is the source of truth?" — that's the question I should have been asking.