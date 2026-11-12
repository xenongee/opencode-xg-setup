## codebase-memory

- Start unfamiliar-code, architecture and refactoring investigations with the graph before broad grep or file reading; call index_status and check freshness for the exact repository root.
- If the repository has no index, call index_repository immediately; recheck freshness after indexing rather than assuming a successful request proves complete coverage.
- Use get_architecture for the overview, then search_graph and get_code_snippet to inspect concrete symbols and their implementation.
- Before changing signatures or contracts, use trace_path in both inbound and outbound directions; verify the relevant callers and downstream effects in source.
- Run detect_changes against the intended diff to inspect its impact radius; do not mistake a clean or partial graph result for proof that all consumers were updated.
- Check check_index_coverage for cited paths; use grep to fill uncovered literals, configuration, generated interfaces and dynamic calls. If graph tools are unavailable, report that gap and trace the same relationships in source.
