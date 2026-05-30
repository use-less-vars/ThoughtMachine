# Frequently Asked Questions

## General

**Q: How do I change the LLM model?**
A: Edit `~/.thoughtmachine/config.json` and set `default_model` to your
desired model name.

**Q: How do I reset my configuration?**
A: Delete `~/.thoughtmachine/config.json` and restart the agent. Defaults
will be restored.

## Knowledge Base

**Q: What is the difference between `scope=workspace` and `scope=global`?**
A: `scope=workspace` accesses project-local knowledge (`.thoughtmachine/knowledge/`).
`scope=global` accesses the user-wide global knowledge base at
`~/.thoughtmachine/knowledge/`.

**Q: Can I create custom domains?**
A: Yes! Use `mode=create_domain domain=my_topic category=personal` to
create a new domain file.

## Tools

**Q: Why did my edit fail?**
A: The ApplyEdits tool uses fuzzy matching. Ensure your `find` text matches
the source exactly, including whitespace and indentation.

**Q: How do I run Python code?**
A: Use `DockerCodeRunner` with `command='python3 -c "..."'` or `script`
for multi-line scripts.
