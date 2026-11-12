## Semgrep

- Run semgrep_scan on changed and new files after touching parsers, networking, subprocess execution, filesystem paths, authorization or serialization; scope the scan to the actual risk-bearing diff.
- Inspect each finding's source, data flow and trust boundary before changing code; fix the shared cause rather than adding blanket ignores or reshaping code solely to silence a rule.
- Use semgrep_scan_with_custom_rule to find repeated anti-patterns or dangerous calls across the project; validate the rule on a known match and a safe counterexample before trusting its coverage.
- Run semgrep_scan_supply_chain when adding or updating dependencies or lockfiles; distinguish a new scan from querying historical findings and report unresolved or unsupported dependency coverage.
- Check the actual exposed tools and execution location before scanning; never send credentials, private environment files or unrelated code to a remote scanner. If an MCP operation is unavailable, use an approved local CLI or state the blocked check explicitly.
- Treat a clean scan as a baseline SAST filter, not proof of safe behavior; still inspect validation, ownership, concurrency and failure handling, and verify the relevant contract directly.
