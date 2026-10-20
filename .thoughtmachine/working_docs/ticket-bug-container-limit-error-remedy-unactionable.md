# bug/container-limit-error-remedy-unactionable

`container_create_failed: container limit reached; remove an existing container
to free a slot` — but when all containers are `lifecycle_class: persistent`,
the agent's `ContainerRemoveTool` refuses each:
`"Persistent containers can only be removed by the operator."`

**The error's remedy is unactionable from the recipient.** Same class as
display-truth: surface tells the caller to do a thing the system won't let
them do.

**Reproduced 2026-10-20** in `gentle-wolf-72`: three removal attempts
(g3cut, g3series, g2-detached) all refused.

**Fix:** error message must name the class problem. Suggested copy:
```
Container limit reached (6/6). All six are persistent-class — only the
operator can remove them. Ask the operator to remove a container, or use a
workspace with fewer persistent containers.
```
Plus: attach the list of containers the operator could remove (name + class
+ last-used) so the remedy is concrete, not abstract.

**Filed by:** Gen 46, 2026-10-20.
