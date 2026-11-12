## Skills & Workflows

- Use ponytail for the minimum complete change, ponytail-review for a diff and its callers, ponytail-audit for repository-wide risk, and ponytail-debt for shortcut: comments.
- Invoke step-back when a fix accumulates workarounds; use simplify to untangle code without changing behavior.
- Use verification-planning to choose evidence for the actual risk, and deanchor to infer principles from examples instead of copying their surface form.
- Use clonedeps when dependency internals must be inspected and worktrees when concurrent writers need isolated checkouts.
- Reserve deepwork for large multi-phase changes; use reflect to turn repeated workflow friction into reusable improvements.
- Use x-diagnosing-bugs for complex or flaky failures before proposing another speculative patch.
- Pair security-threat-model and security-best-practices with Semgrep for threat analysis and security review; a clean scan does not establish a safe design.
- Invoke frontend-design for UI or HTML work; let it shape the layout rather than emitting a generic template.
- Use prototype for a quick experiment that tests an assumption, then retain only the behavior the experiment justified.
- Use mcp-builder to design and implement MCP servers, tools and their evaluation path.
- Use egm-drift-report to find documentation claims that no longer agree with code.
- Invoke grilling to pressure-test an unclear idea through focused questions before implementation.
- Use x-bro for /bro: restate the last message plainly, preserving its facts and conditions without jargon.
